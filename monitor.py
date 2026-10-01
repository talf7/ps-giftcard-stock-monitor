"""Amazon.in stock monitor with Telegram alerts.

Polls an Amazon product page and sends a Telegram message the moment the
item becomes available (and again if it goes out of stock).
"""
import os
import random
import sys
import time
from enum import Enum

import requests

ASIN = os.getenv("ASIN", "B093QF35KZ")  # Rs.2000 PlayStation Store Gift Card
PRODUCT_URL = f"https://www.amazon.in/dp/{ASIN}"
INTERVAL = float(os.getenv("CHECK_INTERVAL", "1"))  # seconds between checks
MAX_RUNTIME = float(os.getenv("MAX_RUNTIME", "0"))  # 0 = run forever
REMIND_EVERY = float(os.getenv("REMIND_EVERY", "60"))  # re-alert while in stock (s)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
]


class Status(Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BLOCKED = "blocked"  # captcha / robot check
    UNKNOWN = "unknown"


def parse_status(html: str) -> Status:
    lower = html.lower()
    if "validatecaptcha" in lower or "enter the characters you see below" in lower:
        return Status.BLOCKED
    if 'id="add-to-cart-button"' in lower or 'id="buy-now-button"' in lower:
        return Status.IN_STOCK
    if "currently unavailable" in lower or 'id="outofstock"' in lower:
        return Status.OUT_OF_STOCK
    return Status.UNKNOWN


def fetch_status(session: requests.Session) -> Status:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    resp = session.get(PRODUCT_URL, headers=headers, timeout=10)
    if resp.status_code == 503:
        return Status.BLOCKED
    if resp.status_code != 200:
        return Status.UNKNOWN
    return parse_status(resp.text)


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[telegram not configured] {text}", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}", flush=True)
    except requests.RequestException as e:
        print(f"Telegram request failed: {e}", flush=True)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def main() -> int:
    session = requests.Session()
    start = time.monotonic()
    last_status = None
    last_alert = 0.0
    backoff = 0.0

    log(f"Monitoring {PRODUCT_URL} every {INTERVAL}s")
    if os.getenv("STARTUP_MESSAGE", "1") == "1":
        send_telegram(f"👀 Stock monitor started for {PRODUCT_URL}")

    while True:
        if MAX_RUNTIME and time.monotonic() - start > MAX_RUNTIME:
            log("Max runtime reached, exiting")
            return 0
        try:
            status = fetch_status(session)
        except requests.RequestException as e:
            log(f"Request error: {e}")
            status = Status.UNKNOWN

        if status is Status.BLOCKED:
            # Amazon rate-limited us: back off exponentially (max 5 min)
            backoff = min(max(backoff * 2, 10), 300)
            session = requests.Session()
            log(f"Blocked by Amazon (captcha/503), backing off {backoff:.0f}s")
            time.sleep(backoff)
            continue
        backoff = 0.0

        now = time.monotonic()
        if status is Status.IN_STOCK:
            if last_status is not Status.IN_STOCK or now - last_alert >= REMIND_EVERY:
                send_telegram(f"🟢 IN STOCK! PlayStation gift card is available:\n{PRODUCT_URL}")
                last_alert = now
        elif status is Status.OUT_OF_STOCK and last_status is Status.IN_STOCK:
            send_telegram(f"🔴 Out of stock again:\n{PRODUCT_URL}")

        if status is not last_status:
            log(f"Status: {status.value}")
        if status is not Status.UNKNOWN:
            last_status = status

        time.sleep(INTERVAL + random.uniform(0, INTERVAL * 0.3))


if __name__ == "__main__":
    sys.exit(main())
