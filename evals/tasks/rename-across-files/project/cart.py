from pricing import format_price


def cart_summary(items):
    total_cents = sum(item["price_cents"] for item in items)
    lines = [f"- {item['name']}: {format_price(item['price_cents'])}"
             for item in items]
    return "\n".join(lines) + f"\n合计: {format_price(total_cents)}"
