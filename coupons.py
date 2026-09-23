"""
coupons.py  (Snagly)

AliExpress tiered discount codes valid for US-shipping orders.
Update this file manually whenever the codes or validity dates change
(check the affiliate portal's promo/code center and the newsletter emails -
these are NOT available through the API).

Thresholds are in USD ($), matching the price US customers see on
AliExpress product pages (target_currency=USD, ship_to_country=US).

⚠️ COUPONS is intentionally empty until you have US codes that were
actually tested at checkout with a US shipping address. While it's empty,
the bot simply shows no coupon lines (product info + link still work).
The Israeli codes (FSIL/ILFS/FAIL etc.) will NOT work for US orders.
"""

from datetime import datetime, timezone, timedelta

ENABLED = True

# AliExpress US promos are usually stated in Pacific Time.
# PDT = UTC-7 (roughly Mar-Nov), PST = UTC-8 (Nov-Mar) - adjust per batch.
PROMO_TZ = timezone(timedelta(hours=-7))
PROMO_TZ_LABEL = "PT"

# Update these every time you refresh the codes.
VALID_FROM = datetime(2026, 9, 22, 0, 0, tzinfo=PROMO_TZ)
VALID_UNTIL = datetime(2026, 9, 30, 23, 59, tzinfo=PROMO_TZ)

# Sorted descending by min_order_usd - first match wins.
# Example shape (NOT real codes):
#   {"code": "XXXX05", "off_usd": 20, "min_order_usd": 159},
#   {"code": "XXXX01", "off_usd": 2, "min_order_usd": 15},
COUPONS = []


def _is_active() -> bool:
    """Feature on, codes exist, and we're between VALID_FROM and VALID_UNTIL."""
    if not ENABLED or not COUPONS:
        return False
    now = datetime.now(timezone.utc)
    return VALID_FROM <= now <= VALID_UNTIL


def urgency_note() -> str:
    """Shown only when the codes expire within 24 hours."""
    if not _is_active():
        return ""
    remaining = (VALID_UNTIL - datetime.now(timezone.utc)).total_seconds()
    if remaining > 24 * 60 * 60:
        return ""
    local = VALID_UNTIL.astimezone(PROMO_TZ)
    # Built manually because "%-I" isn't supported on Windows
    time_str = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return f"⏰ Heads up: this code expires tonight ({time_str} {PROMO_TZ_LABEL}) - worth using today!"


def best_coupon_for_price(price_usd: float):
    """Best coupon this price already qualifies for, or None."""
    if not _is_active():
        return None
    for coupon in COUPONS:
        if price_usd >= coupon["min_order_usd"]:
            return coupon
    return None


def next_tier_gap(price_usd: float):
    """Next tier not yet reached (even if a lower one applies), or None."""
    if not _is_active():
        return None
    ascending = sorted(COUPONS, key=lambda c: c["min_order_usd"])
    for coupon in ascending:
        if price_usd < coupon["min_order_usd"]:
            return {"needed": coupon["min_order_usd"] - price_usd, "coupon": coupon}
    return None


def all_coupons_text() -> str:
    """Every active tier, for when the product's price is unknown."""
    if not _is_active():
        return ""
    ascending = sorted(COUPONS, key=lambda c: c["min_order_usd"])
    return "\n".join(
        f"{c['code']} - ${c['off_usd']} off orders over ${c['min_order_usd']}"
        for c in ascending
    )
