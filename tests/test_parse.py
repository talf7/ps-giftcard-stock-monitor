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
    monkeypatch.setattr(monitor, "send_telegram", lambda t: sent.append(t) or True)
    p = Product("B093QF35KZ", "Rs.2000")
    monitor.handle_result(p, Status.OUT_OF_STOCK, None, 0)
    assert sent == []
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 1)
    assert len(sent) == 1 and "IN STOCK" in sent[0] and "cart/add" in sent[0]
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 2)  # no spam within REMIND_EVERY
    assert len(sent) == 1
    monitor.handle_result(p, Status.IN_STOCK, "2,000", 2 + monitor.REMIND_EVERY)
    assert len(sent) == 2
    monitor.handle_result(p, Status.OUT_OF_STOCK, None, 200)
    assert "Out of stock" in sent[-1]


def test_failed_send_retries(monkeypatch):
    monkeypatch.setattr(monitor, "send_telegram", lambda t: False)
    p = Product("X", "X")
    monitor.handle_result(p, Status.IN_STOCK, None, 5)
    assert p.last_alert == 0.0  # will retry on next check
