from pricing import format_price


def test_format():
    assert format_price(12345) == "¥123.45"


def test_zero():
    assert format_price(0) == "¥0.00"
