from pricing import format_price


def daily_report(orders):
    revenue = sum(o["price_cents"] for o in orders)
    return f"今日 {len(orders)} 单, 营收 {format_price(revenue)}"
