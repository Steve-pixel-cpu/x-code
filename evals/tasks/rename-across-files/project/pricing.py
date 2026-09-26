def format_price(cents):
    """分 -> 显示价, 如 12345 -> "¥123.45"。"""
    return f"¥{cents / 100:.2f}"
