"""Amazon.in stock monitor with Telegram alerts.

Round-robins over the products in products.txt and sends a Telegram message
the moment any of them becomes available (and again if it goes out of stock).
"""
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import requests

try:
    # Mimics Chrome's TLS/HTTP2 fingerprint, which Amazon checks to spot scripts
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover
    browser_requests = None

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so running locally needs no extra setup."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")

PRODUCTS_FILE = Path(os.getenv("PRODUCTS_FILE", HERE / "products.txt"))
INTERVAL = float(os.getenv("CHECK_INTERVAL", "2"))  # seconds between requests
MAX_RUNTIME = float(os.getenv("MAX_RUNTIME", "0"))  # 0 = run forever
REMIND_EVERY = float(os.getenv("REMIND_EVERY", "60"))  # re-alert while in stock (s)
BLOCK_ALERT_AFTER = float(os.getenv("BLOCK_ALERT_AFTER", "600"))  # warn if blocked this long (s)
MAX_BACKOFF = 120
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
]

PRICE_RE = re.compile(r'class="a-price-whole">\s*([\d,]+)')


class Status(Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BLOCKED = "blocked"  # captcha / robot check
    UNKNOWN = "unknown"


@dataclass
class Product:
    asin: str
    label: str
    status: Status | None = None
    last_alert: float = 0.0

    @property
    def url(self) -> str:
        return f"https://www.amazon.in/dp/{self.asin}"

    @property
    def cart_url(self) -> str:
        return f"https://www.amazon.in/gp/aws/cart/add.html?ASIN.1={self.asin}&Quantity.1=1"


def load_products(path: Path) -> list[Product]:
    products = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        asin, _, label = line.partition("|")
        products.append(Product(asin.strip(), label.strip() or asin.strip()))
    return products


def parse_status(html: str) -> Status:
    lower = html.lower()
    if "validatecaptcha" in lower or "enter the characters you see below" in lower:
        return Status.BLOCKED
    if 'id="add-to-cart-button"' in lower or 'id="buy-now-button"' in lower:
        return Status.IN_STOCK
    if "currently unavailable" in lower or 'id="outofstock"' in lower:
        return Status.OUT_OF_STOCK
    return Status.UNKNOWN


# Only look for the price inside the product's own buy box; elsewhere on the
# page prices belong to recommended/sponsored products.
PRICE_BLOCK_IDS = ('id="corePriceDisplay_desktop_feature_div"', 'id="corePrice_feature_div"',
                   'id="corePrice_desktop"', 'id="apex_desktop"')


def parse_price(html: str) -> str | None:
    for block_id in PRICE_BLOCK_IDS:
        start = html.find(block_id)
        if start != -1:
            m = PRICE_RE.search(html, start, start + 5000)
            if m:
                return m.group(1)
    return None


def new_session():
    """Fresh session that first visits the home page to pick up cookies like a browser."""
    if browser_requests is not None:
        session = browser_requests.Session(impersonate="chrome")
    else:
        session = requests.Session()
        session.headers["User-Agent"] = random.choice(USER_AGENTS)
    session.headers["Accept-Language"] = "en-IN,en;q=0.9"
    try:
        session.get("https://www.amazon.in/", timeout=10)
    except Exception as e:
        log(f"Home page warm-up failed: {e}")
    return session


def fetch(session, product: Product) -> tuple[Status, str | None]:
    resp = session.get(product.url, timeout=10)
    if resp.status_code == 503:
        return Status.BLOCKED, None
    if resp.status_code != 200:
        return Status.UNKNOWN, None
    return parse_status(resp.text), parse_price(resp.text)


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[telegram not configured] {text}", flush=True)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}", flush=True)
        return r.ok
    except requests.RequestException as e:
        print(f"Telegram request failed: {e}", flush=True)
        return False


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def keep_awake() -> None:
    """On Windows, stop the PC from sleeping while the monitor runs."""
    if sys.platform == "win32":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def handle_result(product: Product, status: Status, price: str | None, now: float) -> None:
    price_txt = f" — ₹{price}" if price else ""
    if status is Status.IN_STOCK:
        if product.status is not Status.IN_STOCK or now - product.last_alert >= REMIND_EVERY:
            sent = send_telegram(
                f"🟢 IN STOCK: {product.label}{price_txt}\n\n"
                f"🛒 Add to cart: {product.cart_url}\n"
                f"📄 Page: {product.url}"
            )
            if sent:
                product.last_alert = now
    elif status is Status.OUT_OF_STOCK and product.status is Status.IN_STOCK:
        send_telegram(f"🔴 Out of stock again: {product.label}\n{product.url}")

    if status is not product.status and status is not Status.UNKNOWN:
        log(f"{product.label}: {status.value}{price_txt}")
        product.status = status


def main() -> int:
    products = load_products(PRODUCTS_FILE)
    if not products:
        log(f"No products in {PRODUCTS_FILE}")
        return 1
    keep_awake()
    session = new_session()
    start = round_start = time.monotonic()
    rounds = blocks = errors = 0
    backoff = 0.0
    blocked_since = None
    block_alerted = False

    if browser_requests is None:
        log("curl_cffi not installed - expect more blocks (pip install -r requirements.txt)")
    log(f"Monitoring {len(products)} products, one request every ~{INTERVAL}s")
    if os.getenv("STARTUP_MESSAGE", "1") == "1":
        names = "\n".join(f"• {p.label}" for p in products)
        send_telegram(f"👀 Stock monitor started, watching {len(products)} products:\n{names}")

    i = 0
    while True:
        if MAX_RUNTIME and time.monotonic() - start > MAX_RUNTIME:
            log("Max runtime reached, exiting")
            return 0
        product = products[i % len(products)]
        try:
            status, price = fetch(session, product)
        except Exception as e:
            log(f"Request error ({product.label}): {e}")
            status, price = Status.UNKNOWN, None
        if status is Status.UNKNOWN:
            errors += 1

        if status is Status.BLOCKED:
            # Blocks are per IP, so pause everything and retry the same product
            blocks += 1
            now = time.monotonic()
            blocked_since = blocked_since or now
            if not block_alerted and now - blocked_since >= BLOCK_ALERT_AFTER:
                block_alerted = send_telegram(
                    f"⚠️ Amazon has been blocking the monitor for {(now - blocked_since) / 60:.0f} min - "
                    f"stock is NOT being checked right now. Consider raising CHECK_INTERVAL "
                    f"or restarting your router to get a new IP."
                )
            backoff = min(max(backoff * 2, 10), MAX_BACKOFF)
            log(f"Blocked by Amazon (captcha/503), backing off {backoff:.0f}s")
            time.sleep(backoff)
            session = new_session()
            continue
        backoff = 0.0
        if blocked_since is not None:
            log(f"Unblocked after {(time.monotonic() - blocked_since) / 60:.1f} min")
            if block_alerted:
                send_telegram("✅ Monitor is no longer blocked - stock checks resumed.")
            blocked_since = None
            block_alerted = False
        i += 1

        now = time.monotonic()
        handle_result(product, status, price, now)

        if i % len(products) == 0:
            rounds += 1
            in_stock = [p.label for p in products if p.status is Status.IN_STOCK]
            unknown = sum(p.status is None for p in products)
            summary = f"IN STOCK: {', '.join(in_stock)}" if in_stock else "all out of stock"
            if unknown:
                summary += f", {unknown} not readable"
            notes = [f"{n} {what}" for n, what in ((blocks, "blocks"), (errors, "failed checks")) if n]
            log(f"Round {rounds} done in {now - round_start:.0f}s - {summary}"
                + (f" ({', '.join(notes)})" if notes else ""))
            round_start = now
            blocks = errors = 0

        time.sleep(INTERVAL + random.uniform(0, INTERVAL * 0.3))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Stopped")
