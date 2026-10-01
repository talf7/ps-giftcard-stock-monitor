from monitor import Status, parse_status


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
