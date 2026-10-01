import monitor
from monitor import Product, Status, load_products, parse_price, parse_status


def test_in_stock():
    html = '<div><input id="add-to-cart-button" type="submit"></div>'
    assert parse_status(html) is Status.IN_STOCK


def test_buy_now_only():
    assert parse_status('<input id="buy-now-button">') is Status.IN_STOCK


def test_out_of_stock():
    html = '<div id="outOfStock"><span>Currently unavailable.</span></div>'
    assert parse_status(html) is Status.OUT_OF_STOCK


def test_captcha():
    html = '<form action="/errors/validateCaptcha">Enter the characters you see below</form>'
    assert parse_status(html) is Status.BLOCKED


def test_unknown():
    assert parse_status("<html>something else</html>") is Status.UNKNOWN


def test_price():
    html = '<div id="corePriceDisplay_desktop_feature_div"><span class="a-price-whole">2,699<span></div>'
    assert parse_price(html) == "2,699"
    assert parse_price("<html></html>") is None


def test_price_ignores_recommendations():
    # Out-of-stock page: no buy-box price, only a carousel item's price
    html = '<div id="outOfStock">Currently unavailable</div><div class="carousel"><span class="a-price-whole">5,999</span></div>'
    assert parse_price(html) is None


def test_load_products(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("# comment\nB000000001 | Card A\n\n#B000000002 | Disabled\nB000000003\n")
    products = load_products(f)
    assert [(p.asin, p.label) for p in products] == [
        ("B000000001", "Card A"),
        ("B000000003", "B000000003"),
    ]


def test_bundled_products_file():
    products = load_products(monitor.PRODUCTS_FILE)
    assert len(products) >= 10
    assert all(len(p.asin) == 10 for p in products)


def test_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "send_telegram", lambda t, b=None: sent.append((t, b)) or True)
    p = Product("B093QF35KZ", "Rs.2000")
    monitor.handle_result(p, Status.OUT_OF_STOCK, None, 0)
    assert sent == []
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 1)
    assert len(sent) == 1 and "IN STOCK" in sent[0][0]
    assert sent[0][1][0][0]["url"].endswith("ASIN.1=B093QF35KZ&Quantity.1=1")
    assert sent[0][1][1][0]["callback_data"] == "mute:B093QF35KZ"
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 2)  # no spam within REMIND_EVERY
    assert len(sent) == 1
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 2 + monitor.REMIND_EVERY)
    assert len(sent) == 2
    monitor.handle_result(p, Status.OUT_OF_STOCK, None, 200)
    assert "Out of stock" in sent[-1][0]


def test_failed_send_retries(monkeypatch):
    monkeypatch.setattr(monitor, "send_telegram", lambda t, b=None: False)
    p = Product("X", "X")
    monitor.handle_result(p, Status.IN_STOCK, None, 5)
    assert p.last_alert == 0.0  # will retry on next check


class FakeResp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


class FakeSession:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, timeout):
        return self.resp


def test_fetch_reports_unrecognized_page_once(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, "DEBUG_DIR", tmp_path)
    logs = []
    monkeypatch.setattr(monitor, "log", logs.append)
    p = Product("B000000001", "Card")
    session = FakeSession(FakeResp(200, "<html>weird page</html>"))
    assert monitor.fetch(session, p) == (Status.UNKNOWN, None)
    monitor.fetch(session, p)
    assert len(logs) == 1 and "Card" in logs[0]
    assert (tmp_path / "B000000001.html").read_text() == "<html>weird page</html>"
    # recovers, and a later problem is reported again
    monitor.fetch(FakeSession(FakeResp(200, '<input id="add-to-cart-button">')), p)
    monitor.fetch(FakeSession(FakeResp(404)), p)
    assert len(logs) == 2 and "not found" in logs[1]


SWATCHES = (
    '<div id="variation_style_name"><li>Rs.2000 <span>Currently unavailable.</span></li>'
    '<li>Rs.4000</li></div>'
)


def test_in_stock_ignores_other_denominations_unavailable():
    html = SWATCHES + '<div id="availability"><span class="a-color-success"> In stock </span></div>'
    assert parse_status(html) is Status.IN_STOCK


def test_out_of_stock_from_availability_box():
    html = SWATCHES + '<div id="availability"><span>Currently unavailable.</span></div>'
    assert parse_status(html) is Status.OUT_OF_STOCK


def test_swatch_text_alone_is_not_out_of_stock():
    assert parse_status(SWATCHES) is Status.UNKNOWN


def test_other_sellers_count_as_in_stock():
    html = '<div id="availability">Currently unavailable</div><a id="buybox-see-all-buying-choices">See All Buying Options</a>'
    assert parse_status(html) is Status.IN_STOCK


def test_parse_location():
    html = '<span class="nav-line-2" id="glow-ingress-line2">\n  Mumbai 400001‌  </span>'
    assert monitor.parse_location(html).startswith("Mumbai 400001")
    assert monitor.parse_location("<html></html>") is None


def test_find_csrf():
    assert monitor.find_csrf('data-a-modal="{&quot;anti-csrftoken-a2z&quot;:&quot;abc123&quot;}"') == "abc123"
    assert monitor.find_csrf('CSRF_TOKEN : "xyz",') == "xyz"
    assert monitor.find_csrf("nothing") is None


def test_parse_seller():
    assert monitor.parse_seller('<a id="sellerProfileTriggerId" href="#">Express Games</a>') == "Express Games"
    html = '<div id="merchantInfoFeature_feature_div"><span>Sold by</span> <span>Amazon</span></div>'
    assert monitor.parse_seller(html).startswith("Amazon")
    assert monitor.parse_seller("<html></html>") is None


def test_ignored_seller_is_not_alerted(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, "DEBUG_DIR", tmp_path)
    monkeypatch.setattr(monitor, "IGNORE_SELLERS", ["express games"])
    monkeypatch.setattr(monitor, "log", lambda m: None)
    html = '<input id="add-to-cart-button"><a id="sellerProfileTriggerId">Express Games</a>'
    p = Product("B07K6RYVJ5", "Rs.4000")
    assert monitor.fetch(FakeSession(FakeResp(200, html)), p)[0] is Status.OUT_OF_STOCK
    other = html.replace("Express Games", "Appario Retail")
    assert monitor.fetch(FakeSession(FakeResp(200, other)), p)[0] is Status.IN_STOCK
    assert p.seller == "Appario Retail"


def test_mute_until_next_restock(monkeypatch):
    sent, calls = [], []
    monkeypatch.setattr(monitor, "send_telegram", lambda t, b=None: sent.append(t) or True)
    monkeypatch.setattr(monitor, "telegram_api", lambda m, p, timeout=10: calls.append((m, p)) or {})
    monkeypatch.setattr(monitor, "log", lambda m: None)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "42")
    p = Product("B07K6RYVJ5", "Rs.4000")
    products = {p.asin: p}
    monitor.handle_result(p, Status.IN_STOCK, None, 0)
    assert len(sent) == 1

    # taps from another chat are ignored
    query = {"id": "q1", "data": "mute:B07K6RYVJ5", "message": {"chat": {"id": 7}, "message_id": 5}}
    monitor.handle_callback(query, products)
    assert not p.muted and calls == []

    query["message"]["chat"]["id"] = 42
    monitor.handle_callback(query, products)
    assert p.muted
    assert calls[0][0] == "answerCallbackQuery"
    assert calls[1][0] == "editMessageReplyMarkup"
    assert calls[1][1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == "unmute:B07K6RYVJ5"

    # still "in stock" on the page, but muted: no reminders
    monitor.handle_result(p, Status.IN_STOCK, None, 10_000)
    assert len(sent) == 1

    # goes out of stock -> unmuted, next restock alerts again
    monitor.handle_result(p, Status.OUT_OF_STOCK, None, 10_001)
    assert not p.muted and "back on" in sent[-1]
    monitor.handle_result(p, Status.IN_STOCK, None, 10_002)
    assert "IN STOCK" in sent[-1]


def test_unmute_button(monkeypatch):
    monkeypatch.setattr(monitor, "telegram_api", lambda m, p, timeout=10: {})
    monkeypatch.setattr(monitor, "log", lambda m: None)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "42")
    p = Product("X", "X", muted=True)
    monitor.handle_callback({"id": "q", "data": "unmute:X",
                             "message": {"chat": {"id": 42}, "message_id": 1}}, {"X": p})
    assert not p.muted
