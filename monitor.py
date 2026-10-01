"""Amazon.in stock monitor with Telegram alerts.

Round-robins over the products in products.txt and sends a Telegram message
the moment any of them becomes available (and again if it goes out of stock).
"""
import os
import random
import re
import sys
import threading
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
INTERVAL = float(os.getenv("CHECK_INTERVAL", "1.5"))  # seconds between request starts
MAX_RUNTIME = float(os.getenv("MAX_RUNTIME", "0"))  # 0 = run forever
REMIND_EVERY = float(os.getenv("REMIND_EVERY", "300"))  # re-alert while in stock (s)
BLOCK_ALERT_AFTER = float(os.getenv("BLOCK_ALERT_AFTER", "600"))  # warn if blocked this long (s)
MAX_BACKOFF = 120
# Amazon shows stock for a delivery location; without an Indian pincode it may hide offers
DELIVERY_PINCODE = os.getenv("DELIVERY_PINCODE", "400001")  # Mumbai
# Sellers whose "in stock" offers are ignored, e.g. listings that fail at checkout
IGNORE_SELLERS = [x.strip().lower() for x in os.getenv("IGNORE_SELLERS", "").split(",") if x.strip()]
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
    last_problem: str | None = None
    seller: str | None = None
    muted: bool = False  # muted from Telegram until the product goes out of stock

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


TAG_RE = re.compile(r"<[^>]+>")


def section_text(html: str, element_id: str, length: int = 3000, lower: bool = True) -> str | None:
    """Visible text right after the element with this id, or None."""
    start = html.find(f'id="{element_id}"')
    if start == -1:
        return None
    start = html.find(">", start) + 1
    text = " ".join(TAG_RE.sub(" ", html[start:start + length]).split())
    return text.lower() if lower else text


SELLER_RE = re.compile(r'id="sellerProfileTriggerId"[^>]*>([^<]+)<')


def parse_seller(html: str) -> str | None:
    m = SELLER_RE.search(html)
    if m:
        return " ".join(m.group(1).split()) or None
    text = section_text(html, "merchantInfoFeature_feature_div", 600, lower=False)
    if text:
        text = re.sub(r"^(sold by|seller)\s*", "", text, flags=re.I)
        return text[:50] or None
    return None


def is_ignored_seller(seller: str | None) -> bool:
    return bool(seller) and any(name in seller.lower() for name in IGNORE_SELLERS)


def parse_status(html: str) -> Status:
    lower = html.lower()
    if "validatecaptcha" in lower or "enter the characters you see below" in lower:
        return Status.BLOCKED
    if 'id="add-to-cart-button"' in lower or 'id="buy-now-button"' in lower:
        return Status.IN_STOCK
    # No featured offer, but other sellers have it
    if 'id="buybox-see-all-buying-choices"' in lower:
        return Status.IN_STOCK
    # Only trust the product's own availability box: "Currently unavailable"
    # also appears next to other denominations listed on the same page.
    availability = section_text(html, "availability", 600)
    if availability:
        if "unavailable" in availability or "out of stock" in availability:
            return Status.OUT_OF_STOCK
        if "in stock" in availability:
            return Status.IN_STOCK
    if 'id="outofstock"' in lower:
        return Status.OUT_OF_STOCK
    return Status.UNKNOWN


LOCATION_RE = re.compile(r'id="glow-ingress-line2"[^>]*>([^<]*)<')


def parse_location(html: str) -> str | None:
    m = LOCATION_RE.search(html)
    return " ".join(m.group(1).split()) or None if m else None


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


location_ok = False  # Amazon confirmed our delivery pincode
last_location_try = float("-inf")


def ensure_location(session) -> None:
    """Set the delivery pincode once Amazon is answering normally (retried every 2 min).

    Done lazily rather than on every new session: while Amazon shows a captcha
    the home page has no token, and the extra requests only prolong the block.
    """
    global last_location_try
    if not DELIVERY_PINCODE or location_ok or time.monotonic() - last_location_try < 120:
        return
    last_location_try = time.monotonic()
    try:
        resp = session.get("https://www.amazon.in/", timeout=10)
        if resp.status_code == 503 or parse_status(resp.text) is Status.BLOCKED:
            return  # try again later
        if set_delivery_location(session, resp.text, DELIVERY_PINCODE):
            log(f"Delivery pincode set to {DELIVERY_PINCODE}")
    except Exception as e:
        log(f"Could not set delivery pincode: {e}")


CSRF_RES = [
    re.compile(r'anti-csrftoken-a2z&quot;:&quot;([^&]+)&quot;'),
    re.compile(r'"anti-csrftoken-a2z"\s*:\s*"([^"]+)"'),
    re.compile(r'CSRF_TOKEN\s*:\s*"([^"]+)"'),
]


def find_csrf(html: str) -> str | None:
    for regex in CSRF_RES:
        m = regex.search(html)
        if m:
            return m.group(1)
    return None


def set_delivery_location(session, home_html: str, pincode: str) -> bool:
    """Set the delivery pincode the same way the "Deliver to" popup on amazon.in does."""
    token = find_csrf(home_html)
    if not token:
        log("Could not set delivery pincode (no token on home page)")
        return False
    modal = session.get(
        "https://www.amazon.in/portal-migration/hz/glow/get-rendered-address-selections"
        "?deviceType=desktop&pageType=Gateway&storeContext=NoStoreName&actionSource=desktop-modal",
        headers={"anti-csrftoken-a2z": token}, timeout=10,
    ).text
    token = find_csrf(modal) or token
    resp = session.post(
        "https://www.amazon.in/portal-migration/hz/glow/address-change?actionSource=glow",
        json={"locationType": "LOCATION_INPUT", "zipCode": pincode, "storeContext": "generic",
              "deviceType": "web", "pageType": "Gateway", "actionSource": "glow"},
        headers={"anti-csrftoken-a2z": token, "Content-Type": "application/json"}, timeout=10,
    )
    ok = resp.status_code == 200 and '"isAddressUpdated":1' in resp.text.replace(" ", "")
    if not ok:
        log(f"Could not set delivery pincode {pincode} (HTTP {resp.status_code})")
    return ok


DEBUG_DIR = HERE / "debug"


last_location = None


def save_page(product: Product, html: str) -> None:
    """Keep the latest page per product in debug/ so detection can be checked by eye."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{product.asin}.html").write_text(html, encoding="utf-8")
    except OSError:
        pass


def report_problem(product: Product, problem: str | None) -> None:
    """Log why a product couldn't be read, once per distinct problem."""
    if problem == product.last_problem:
        return
    product.last_problem = problem
    if problem is not None:
        log(f"{product.label}: could not read stock ({problem})")


def fetch(session, product: Product) -> tuple[Status, str | None]:
    resp = session.get(product.url, timeout=10)
    if resp.status_code == 503:
        return Status.BLOCKED, None
    if resp.status_code != 200:
        problem = "page not found - listing may be removed" if resp.status_code == 404 else f"HTTP {resp.status_code}"
        report_problem(product, problem)
        return Status.UNKNOWN, None
    global last_location, location_ok
    save_page(product, resp.text)
    location = parse_location(resp.text)
    if location and location != last_location:
        log(f"Amazon delivery location: {location}")
        last_location = location
    location_ok = bool(location and DELIVERY_PINCODE and DELIVERY_PINCODE in location)
    status = parse_status(resp.text)
    report_problem(product, "page not recognized, see debug folder" if status is Status.UNKNOWN else None)
    seller = parse_seller(resp.text) if status is Status.IN_STOCK else None
    if is_ignored_seller(seller):
        if product.seller != seller:
            log(f"{product.label}: only offered by ignored seller {seller} - not alerting")
        status = Status.OUT_OF_STOCK
    product.seller = seller
    return status, parse_price(resp.text)


def telegram_api(method: str, payload: dict, timeout: float = 10):
    """Call a Telegram Bot API method; returns the parsed JSON, or None on failure."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                          json=payload, timeout=timeout)
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}", flush=True)
            return None
        return r.json()
    except requests.RequestException as e:
        print(f"Telegram request failed: {e}", flush=True)
        return None


def send_telegram(text: str, buttons: list | None = None) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[telegram not configured] {text}", flush=True)
        return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return telegram_api("sendMessage", payload) is not None


def alert_buttons(product: Product) -> list:
    action = (f"🔔 Unmute", f"unmute:{product.asin}") if product.muted else \
             (f"🔕 Mute until next restock", f"mute:{product.asin}")
    return [
        [{"text": "🛒 Add to cart", "url": product.cart_url}],
        [{"text": action[0], "callback_data": action[1]}],
    ]


def handle_callback(query: dict, products: dict[str, Product]) -> None:
    """React to a tap on an alert's Mute/Unmute button."""
    message = query.get("message") or {}
    if str(message.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
        return
    action, _, asin = (query.get("data") or "").partition(":")
    product = products.get(asin)
    if product is None or action not in ("mute", "unmute"):
        telegram_api("answerCallbackQuery", {"callback_query_id": query["id"]})
        return
    product.muted = action == "mute"
    if product.muted:
        note = f"🔕 Muted {product.label} until it goes out of stock and comes back"
    else:
        note = f"🔔 Alerts for {product.label} are back on"
    log(note)
    telegram_api("answerCallbackQuery", {"callback_query_id": query["id"], "text": note})
    telegram_api("editMessageReplyMarkup", {
        "chat_id": message["chat"]["id"], "message_id": message["message_id"],
        "reply_markup": {"inline_keyboard": alert_buttons(product)},
    })


def poll_telegram(products: dict[str, Product]) -> None:
    """Background loop that receives button taps (long polling)."""
    offset = None
    while True:
        result = telegram_api("getUpdates", {"timeout": 30, "offset": offset,
                                             "allowed_updates": ["callback_query"]}, timeout=40)
        if result is None:
            time.sleep(5)
            continue
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            if "callback_query" in update:
                try:
                    handle_callback(update["callback_query"], products)
                except Exception as e:
                    log(f"Button handling failed: {e}")


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
        due = product.status is not Status.IN_STOCK or now - product.last_alert >= REMIND_EVERY
        if due and not product.muted:
            seller_txt = f"\n🏪 Seller: {product.seller}" if product.seller else ""
            sent = send_telegram(
                f"🟢 IN STOCK: {product.label}{price_txt}{seller_txt}\n\n"
                f"📄 Page: {product.url}",
                alert_buttons(product),
            )
            if sent:
                product.last_alert = now
    elif status is Status.OUT_OF_STOCK and product.status is Status.IN_STOCK:
        was_muted, product.muted = product.muted, False
        send_telegram(f"🔴 Out of stock again: {product.label}"
                      + ("\n🔔 Alerts are back on for the next restock" if was_muted else "")
                      + f"\n{product.url}")

    if status is not product.status and status is not Status.UNKNOWN:
        log(f"{product.label}: {status.value}{price_txt}")
        product.status = status


def main() -> int:
    global location_ok
    products = load_products(PRODUCTS_FILE)
    if not products:
        log(f"No products in {PRODUCTS_FILE}")
        return 1
    keep_awake()
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=poll_telegram, args=({p.asin: p for p in products},),
                         daemon=True).start()
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
        request_start = time.monotonic()
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
            location_ok = False  # new session, new cookies
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
        ensure_location(session)

        if i % len(products) == 0:
            rounds += 1
            in_stock = [p.label + (" (muted)" if p.muted else "")
                        for p in products if p.status is Status.IN_STOCK]
            unknown = sum(p.status is None for p in products)
            summary = f"IN STOCK: {', '.join(in_stock)}" if in_stock else "all out of stock"
            if unknown:
                summary += f", {unknown} not readable"
            notes = [f"{n} {what}" for n, what in ((blocks, "blocks"), (errors, "failed checks")) if n]
            log(f"Round {rounds} done in {now - round_start:.0f}s - {summary}"
                + (f" ({', '.join(notes)})" if notes else ""))
            round_start = now
            blocks = errors = 0

        # Pace from the start of the request, so slow responses don't add extra delay
        elapsed = time.monotonic() - request_start
        time.sleep(max(0.0, INTERVAL - elapsed) + random.uniform(0, 0.4))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Stopped")
