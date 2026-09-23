"""
bot.py - Snagly Telegram bot (US)

Users send AliExpress product link(s) -> the bot replies with product
details (USD), an affiliate tracking link, and any matching discount code.

Products accumulate in a persistent per-user "cart" across separate
messages so coupon eligibility is checked against everything the user has
added so far. Users can clear their cart any time via the
"🗑️ Clear cart" button or /clear.

US version notes:
- Single price fetch per item (USD only) instead of two (ILS + USD) -
  halves the API calls per link compared to the Israeli bot.
- ship_to_country="US" everywhere.
- Admin-facing messages (admin group notifications, /stats) stay in Hebrew.
"""

import asyncio
import logging
import os
import re
import time
from collections import deque
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aliexpress_client import AliExpressClient, AliExpressAPIError
from coupons import best_coupon_for_price, next_tier_gap, urgency_note, VALID_UNTIL
from alternative_finder import ProductRef, find_cheaper_alternatives, format_alternatives_message, format_stats_summary
from stats import BotStats

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# .strip() חשוב: רווח/שורה חדשה שנדבקים בטעות בהדבקה ל-Railway גורמים
# ל-"IncompleteSignature" מאליאקספרס, כי הם נכנסים לחישוב החתימה.
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
ALIEXPRESS_APP_KEY = os.environ["ALIEXPRESS_APP_KEY"].strip()
ALIEXPRESS_APP_SECRET = os.environ["ALIEXPRESS_APP_SECRET"].strip()
ALIEXPRESS_TRACKING_ID = os.environ.get("ALIEXPRESS_TRACKING_ID", "default").strip()

logger.info(
    "Loaded credentials - app_key=%s (len %d), secret length=%d, tracking_id=%r",
    ALIEXPRESS_APP_KEY, len(ALIEXPRESS_APP_KEY), len(ALIEXPRESS_APP_SECRET), ALIEXPRESS_TRACKING_ID,
)

# אופציונלי: צ'אט/קבוצת מעקב לאדמין (ר' /chatid). אם לא מוגדר - כבוי.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

# אופציונלי: מזהי משתמש נוספים (מופרדים בפסיק) שמורשים להריץ /stats מצ'אט פרטי.
ADMIN_USER_IDS = {
    uid.strip() for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid.strip()
}

# עדכונים מקבילים - משתמש אחד שנתקע ב-retries לא חוסם את כולם.
CONCURRENT_UPDATES = 32

STATS = BotStats(currency_symbol="$", label="🇺🇸")

# אופציונלי: proxy עם IP אמריקאי. בד"כ לא נדרש בגרסה האמריקאית אם השרת
# ב-Railway רץ באזור US. אם אחד מהארבעה חסר - אין proxy.
PROXY_USERNAME = os.environ.get("PROXY_USERNAME")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD")
PROXY_HOST = os.environ.get("PROXY_HOST")
PROXY_PORT = os.environ.get("PROXY_PORT")

PROXY_URL = None
if PROXY_USERNAME and PROXY_PASSWORD and PROXY_HOST and PROXY_PORT:
    PROXY_URL = f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"

ali_client = AliExpressClient(
    app_key=ALIEXPRESS_APP_KEY,
    app_secret=ALIEXPRESS_APP_SECRET,
    tracking_id=ALIEXPRESS_TRACKING_ID,
    proxy_url=PROXY_URL,
)
logger.info("US proxy for resolve_short_link: %s", "ENABLED" if PROXY_URL else "disabled")

SHIP_TO_COUNTRY = "US"
CURRENCY = "USD"

# כולל aliexpress.us - משתמשים אמריקאים ידביקו הרבה קישורים מהדומיין הזה
ALIEXPRESS_URL_PATTERN = re.compile(
    r"https?://(?:[\w-]+\.)?aliexpress\.(?:com|us)/\S+|https?://s\.click\.aliexpress\.com/\S+",
    re.IGNORECASE,
)

MAX_LINKS_PER_MESSAGE = 10
CART_ITEM_TTL_SECONDS = 7 * 24 * 60 * 60  # silently prune items older than 7 days

RATE_LIMIT_MAX_LINKS = 10
RATE_LIMIT_WINDOW_SECONDS = 60

ALT_SEARCH_RATE_LIMIT_MAX = 5
ALT_SEARCH_RATE_LIMIT_WINDOW_SECONDS = 120

CLEAR_CART_CALLBACK = "clear_cart"
CLEAR_CART_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🗑️ Clear cart", callback_data=CLEAR_CART_CALLBACK)]]
)

FIND_ALT_CALLBACK_PREFIX = "find_alt:"
ADD_TO_CART_CALLBACK_PREFIX = "add_cart:"

PRICE_DISCLAIMER = (
    "Prices shown are before shipping, sales tax and any import fees - "
    "please check the final total at checkout."
)


def _usd(amount) -> str:
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


REFERRAL_PREFIX = "ref_"


def _bot_link(bot_username: str, referrer_id=None) -> str:
    link = f"https://t.me/{bot_username}"
    if referrer_id:
        link += f"?start={REFERRAL_PREFIX}{referrer_id}"
    return link


def _build_item_keyboard(item_id: str, already_in_cart: bool = False, show_find_alt: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if not already_in_cart:
        rows.append([InlineKeyboardButton("🛒 Add to cart", callback_data=f"{ADD_TO_CART_CALLBACK_PREFIX}{item_id}")])
    if show_find_alt:
        rows.append([InlineKeyboardButton("🔍 Find a cheaper alternative", callback_data=f"{FIND_ALT_CALLBACK_PREFIX}{item_id}")])
    rows.append([InlineKeyboardButton("🗑️ Clear cart", callback_data=CLEAR_CART_CALLBACK)])
    return InlineKeyboardMarkup(rows)


def _current_item_keyboard(context: ContextTypes.DEFAULT_TYPE, item_id: str, user_id=None) -> InlineKeyboardMarkup:
    cart = _get_cart(context)
    done = context.user_data.setdefault("alt_search_done", set())
    return _build_item_keyboard(
        item_id,
        already_in_cart=item_id in cart,
        show_find_alt=item_id not in done,
    )


def _utf16_len(s: str) -> int:
    """אורך ביחידות UTF-16 - כך טלגרם סופר offset/length של entities."""
    return len(s.encode("utf-16-le")) // 2


async def _notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, entities=None) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, entities=entities)
    except Exception:
        logger.warning("Could not send admin notification", exc_info=True)


# --------------------------------------------------------------------------- #
# Cart helpers
# --------------------------------------------------------------------------- #
def _get_cart(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("cart", {})


def _prune_cart(cart: dict) -> None:
    cutoff = time.time() - CART_ITEM_TTL_SECONDS
    stale_ids = [item_id for item_id, item in cart.items() if item["added_at"] < cutoff]
    for item_id in stale_ids:
        del cart[item_id]


def _cart_total_usd(cart: dict) -> float:
    return sum(item["price_usd"] for item in cart.values())


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def _check_rate_limit(context: ContextTypes.DEFAULT_TYPE, new_links_count: int):
    now = time.time()
    timestamps = context.user_data.setdefault("link_timestamps", deque())
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SECONDS:
        timestamps.popleft()
    if len(timestamps) + new_links_count > RATE_LIMIT_MAX_LINKS:
        retry_after = RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0]) if timestamps else RATE_LIMIT_WINDOW_SECONDS
        return False, max(retry_after, 1)
    return True, 0


def _record_rate_limit(context: ContextTypes.DEFAULT_TYPE, links_count: int) -> None:
    now = time.time()
    timestamps = context.user_data.setdefault("link_timestamps", deque())
    for _ in range(links_count):
        timestamps.append(now)


def _check_alt_search_rate_limit(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    timestamps = context.user_data.setdefault("alt_search_timestamps", deque())
    while timestamps and now - timestamps[0] > ALT_SEARCH_RATE_LIMIT_WINDOW_SECONDS:
        timestamps.popleft()
    if len(timestamps) >= ALT_SEARCH_RATE_LIMIT_MAX:
        retry_after = ALT_SEARCH_RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0])
        return False, max(retry_after, 1)
    return True, 0


def _record_alt_search_rate_limit(context: ContextTypes.DEFAULT_TYPE) -> None:
    timestamps = context.user_data.setdefault("alt_search_timestamps", deque())
    timestamps.append(time.time())


# --------------------------------------------------------------------------- #
# Coupon / cart text blocks
# --------------------------------------------------------------------------- #
def _format_solo_coupon_hint(price_usd: float) -> str:
    """Coupon hint based on THIS item's price alone (before it's added to the cart)."""
    lines = []

    coupon = best_coupon_for_price(price_usd)
    if coupon:
        lines.append(
            f"🎁 Discount code for this item on its own: {coupon['code']} - "
            f"{_usd(coupon['off_usd'])} off orders over {_usd(coupon['min_order_usd'])}"
        )

    gap = next_tier_gap(price_usd)
    if gap:
        lines.append(
            f"💡 Add {_usd(gap['needed'])} more to unlock {gap['coupon']['code']} "
            f"({_usd(gap['coupon']['off_usd'])} off orders over {_usd(gap['coupon']['min_order_usd'])})"
        )

    if lines:
        note = urgency_note()
        if note:
            lines.append(note)

    return "\n".join(lines)


def _format_coupon_block(cart: dict) -> str:
    """Coupon match + next-tier upsell based on the FULL cart. Shown BEFORE the link."""
    if not cart:
        return ""

    total = _cart_total_usd(cart)
    count = len(cart)
    lines = []

    basis_note = f" (based on {count} items in your cart: {_usd(total)})" if count > 1 else ""

    coupon = best_coupon_for_price(total)
    if coupon:
        lines.append(
            f"🎁 Matching discount code{basis_note}: {coupon['code']} - "
            f"{_usd(coupon['off_usd'])} off orders over {_usd(coupon['min_order_usd'])}"
        )

    gap = next_tier_gap(total)
    if gap:
        lines.append(
            f"💡 {_usd(gap['needed'])} more{basis_note} unlocks "
            f"{gap['coupon']['code']} ({_usd(gap['coupon']['off_usd'])} off orders "
            f"over {_usd(gap['coupon']['min_order_usd'])}) - send me another item!"
        )

    if lines:
        note = urgency_note()
        if note:
            lines.append(note)

    return "\n".join(lines)


def _format_cart_summary(cart: dict) -> str:
    """Cart totals + how-to-redeem + /cart hint. Shown AFTER the link."""
    if not cart:
        return ""

    total = _cart_total_usd(cart)
    count = len(cart)
    item_word = "item" if count == 1 else "items"
    lines = [f"🛒 Your cart: {count} {item_word}, total {_usd(total)}"]

    coupon = best_coupon_for_price(total)
    if coupon:
        valid_until_str = VALID_UNTIL.strftime("%b %d, %Y")
        lines.append(
            f"⚠️ Open AliExpress through one of the links I sent, add everything "
            f"to your cart there, and enter {coupon['code']} at checkout. "
            f"Valid until {valid_until_str}.\n"
            f"Note: the price shown here is usually the cheapest variant "
            f"(size/color/quantity) - if you pick a different one, the price "
            f"may change. Check your final total before applying the code."
        )

    lines.append("\nUse /cart to see your whole cart, or clear it with the button below 👇")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fetching product info
# --------------------------------------------------------------------------- #
def _looks_like_foreign_routing(url: str) -> bool:
    """
    למעקב/stats בלבד: האם קישור נפתר לגרסה אזורית לא-אמריקאית.
    בגרסה האמריקאית aliexpress.us ו-glo2usa הם הניתוב ה"נכון".
    """
    if "aliexpress.us" in url:
        return False
    match = re.search(r"gatewayAdapt=glo2(\w+)", url)
    if match and not match.group(1).startswith("usa"):
        return True
    return False


def _fetch_item(raw_url: str):
    """
    Resolves a URL to an item, fetches USD pricing, and generates an
    affiliate link. Returns a dict or None if the item couldn't be resolved.
    """
    full_url = raw_url
    item_id = ali_client.extract_item_id(full_url)
    if not item_id:
        full_url = ali_client.resolve_short_link(raw_url)
        item_id = ali_client.extract_item_id(full_url)

    if not item_id:
        logger.warning(
            "Could not extract item_id even after resolving short link. "
            "raw_url=%s resolved_url=%s", raw_url, full_url
        )
        return None

    product = ali_client.get_product_details(item_id, target_currency=CURRENCY, ship_to_country=SHIP_TO_COUNTRY)
    ships_to_us = True

    if not product:
        # Might be filtered out by ship_to_country=US (doesn't ship to the US)
        # rather than a real failure - retry without the filter to tell them apart.
        logger.warning(
            "get_product_details returned nothing with ship_to_country=US for "
            "item_id=%s - retrying without the country filter", item_id
        )
        product = ali_client.get_product_details(item_id, target_currency=CURRENCY, ship_to_country=None)
        ships_to_us = False

        if not product:
            logger.warning(
                "get_product_details still empty for item_id=%s - likely not in "
                "the affiliate catalog. Falling back to a link-only reply.", item_id
            )
            foreign_routing = _looks_like_foreign_routing(full_url)
            if foreign_routing:
                logger.info("item_id=%s: resolved URL looks like foreign-region routing: %s", item_id, full_url)
            return _fetch_item_minimal(raw_url, item_id, foreign_routing_suspected=foreign_routing)

    price_usd = product.get("target_sale_price", product.get("target_original_price"))
    if price_usd is None:
        logger.warning("No usable price field for item_id=%s: %s", item_id, product)
        return _fetch_item_minimal(raw_url, item_id)

    is_affiliate = True
    try:
        affiliate_link = ali_client.generate_affiliate_link(
            f"https://www.aliexpress.com/item/{item_id}.html"
        )
    except AliExpressAPIError:
        logger.warning(
            "Could not generate affiliate link for item %s - falling back to original URL",
            item_id, exc_info=True,
        )
        affiliate_link = raw_url
        is_affiliate = False

    image_url = product.get("product_main_image_url")
    if image_url and image_url.startswith("//"):
        image_url = "https:" + image_url

    return {
        "item_id": item_id,
        "title": product.get("product_title", "Product"),
        "price_usd": float(price_usd),
        "currency": product.get("target_sale_price_currency", CURRENCY),
        "original_price": product.get("target_original_price"),
        "discount": product.get("discount"),
        "rating": product.get("evaluate_rate", ""),
        "ships_to_us": ships_to_us,
        "image_url": image_url,
        "link": affiliate_link,
        "raw_url": raw_url,
        "is_affiliate": is_affiliate,
        "catalog_available": True,
        "category_id": product.get("first_level_category_id"),
        "added_at": time.time(),
    }


def _fetch_item_timed(raw_url: str):
    """רץ ב-thread (asyncio.to_thread) - כל ה-I/O החוסם מחוץ ל-event loop. מחזיר (item, seconds)."""
    started = time.monotonic()
    item = _fetch_item(raw_url)
    return item, time.monotonic() - started


def _fetch_item_minimal(raw_url: str, item_id: str, foreign_routing_suspected: bool = False):
    """Product not in the affiliate catalog - no price/title/image, link only."""
    try:
        link = ali_client.generate_affiliate_link(f"https://www.aliexpress.com/item/{item_id}.html")
        is_affiliate = True
    except AliExpressAPIError:
        logger.warning(
            "Could not generate affiliate link for catalog-less item %s either", item_id, exc_info=True
        )
        link = raw_url
        is_affiliate = False

    return {
        "item_id": item_id,
        "title": None,
        "price_usd": None,
        "currency": None,
        "original_price": None,
        "discount": None,
        "rating": None,
        "ships_to_us": None,
        "image_url": None,
        "link": link,
        "raw_url": raw_url,
        "foreign_routing_suspected": foreign_routing_suspected,
        "is_affiliate": is_affiliate,
        "catalog_available": False,
        "category_id": None,
        "added_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    STATS.record_start(user)
    STATS.seen_user(user)
    if user:
        context.bot_data.setdefault("user_names", {})[user.id] = (
            f"@{user.username}" if user.username else user.full_name
        )

    if context.args and context.args[0].startswith(REFERRAL_PREFIX):
        ref_raw = context.args[0][len(REFERRAL_PREFIX):]
        if ref_raw.isdigit():
            referrer_id = int(ref_raw)
            referrer_name = context.bot_data.setdefault("user_names", {}).get(referrer_id, "")
            if STATS.record_referral(referrer_id, referrer_name, user):
                who = f"@{user.username}" if user and user.username else (user.full_name if user else "?")
                await _notify_admin(
                    context, f"🇺🇸 🤝 משתמש חדש הגיע דרך שיתוף: {who} (שיתף: {referrer_name or referrer_id})"
                )

    await update.message.reply_text(
        "Hey! 👋 I'm Snagly.\n\n"
        "Send me a link (or a few) to any AliExpress product and I'll send back "
        "the price, rating, a direct link - and a matching discount code when "
        "there is one 🎁\n\n"
        "I remember every item you add, even across separate messages - so if "
        "you're buying a few small things, I'll still find the biggest discount "
        "that fits your total. Use /cart to see your cart and /clear (or the "
        "button) to empty it.\n\n"
        "Just paste a link to get started 🔗\n\n"
        "Disclosure: the links I send are affiliate links - if you buy through "
        "them, Snagly may earn a small commission at no extra cost to you."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me an AliExpress product link (any format) and I'll take care of it.\n\n"
        "Commands:\n"
        "/cart - show everything in your cart\n"
        "/clear - empty your cart"
    )


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"🆔 chat_id: {chat.id}")


def _is_admin(update: Update) -> bool:
    """/stats לאדמין בלבד. אם ADMIN_CHAT_ID ו-ADMIN_USER_IDS לא מוגדרים - פתוח (הרצה מקומית)."""
    if not ADMIN_CHAT_ID and not ADMIN_USER_IDS:
        return True
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == ADMIN_CHAT_ID or user_id == ADMIN_CHAT_ID or user_id in ADMIN_USER_IDS


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """דוח שימוש מלא לאדמין (בעברית, ר' stats.py). מתאפס בכל redeploy."""
    if not _is_admin(update):
        return

    active_carts = sum(1 for ud in context.application.user_data.values() if ud.get("cart"))
    pending_users = sum(1 for ud in context.application.user_data.values() if ud.get("pending_items"))

    report = STATS.format_report(
        ali_client.get_api_stats(), active_carts=active_carts, pending_users=pending_users
    )
    for i in range(0, len(report), 4000):
        await update.message.reply_text(report[i:i + 4000])


async def cart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATS.inc("cart_views")
    cart = _get_cart(context)
    _prune_cart(cart)

    if not cart:
        await update.message.reply_text("Your cart is empty. Send me a product link to get started 🔗")
        return

    lines = ["🛒 Items in your cart:\n"]
    for i, item in enumerate(cart.values(), start=1):
        ship_note = "" if item.get("ships_to_us", True) else " ⚠️ (US shipping not confirmed)"
        lines.append(f"{i}. {item['title']}\n   💰 {_usd(item['price_usd'])}{ship_note}")

    coupon_block = _format_coupon_block(cart)
    if coupon_block:
        lines.append("\n" + coupon_block)

    lines.append("\n🔗 Product links:")
    for i, item in enumerate(cart.values(), start=1):
        lines.append(f"{i}. {item['link']}")

    lines.append("\n" + _format_cart_summary(cart))

    await update.message.reply_text("\n".join(lines), reply_markup=CLEAR_CART_KEYBOARD)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATS.inc("cart_clears")
    context.user_data["cart"] = {}
    context.user_data["alt_search_done"] = set()
    await update.message.reply_text("Cart cleared 🧹")


async def clear_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    STATS.inc("cart_clears")
    context.user_data["cart"] = {}
    context.user_data["alt_search_done"] = set()
    try:
        await query.edit_message_text("Cart cleared 🧹")
    except Exception:
        await query.message.reply_text("Cart cleared 🧹")


async def add_to_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    item_id = query.data[len(ADD_TO_CART_CALLBACK_PREFIX):]

    pending = context.user_data.setdefault("pending_items", {})
    item = pending.get(item_id)

    if not item:
        await query.answer(
            "I couldn't find this item anymore - it probably expired. Please send the link again.",
            show_alert=True,
        )
        return

    cart = _get_cart(context)
    already_there = item_id in cart
    cart[item_id] = item
    _prune_cart(cart)
    if not already_there:
        STATS.inc("add_to_cart")
        STATS.inc("cart_value_added", item.get("price_usd") or 0)

    await query.answer("Added to cart! 🛒")
    await query.message.reply_text(f"✅ Added to your cart.\n\n{_format_cart_summary(cart)}")

    try:
        await query.message.edit_reply_markup(
            reply_markup=_current_item_keyboard(context, item_id, query.from_user.id)
        )
    except Exception:
        logger.warning("Could not update keyboard after add-to-cart for item_id=%s", item_id)


async def find_alternative_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    item_id = query.data[len(FIND_ALT_CALLBACK_PREFIX):]

    done = context.user_data.setdefault("alt_search_done", set())
    if item_id in done:
        await query.answer("You already searched for an alternative to this item.", show_alert=True)
        return

    in_progress = context.user_data.setdefault("alt_search_in_progress", set())
    if item_id in in_progress:
        await query.answer("Already searching for this one, hang on... ⏳")
        return

    allowed, retry_after = _check_alt_search_rate_limit(context)
    if not allowed:
        STATS.inc("alt_rate_limited")
        await query.answer(
            f"Too many searches in a short time - try again in {int(retry_after)} seconds",
            show_alert=True,
        )
        return

    # מסמנים "בתהליך" לפני כל await - מונע לחיצה כפולה מקבילה
    in_progress.add(item_id)
    _record_alt_search_rate_limit(context)
    try:
        await query.answer()

        cart = _get_cart(context)
        pending = context.user_data.setdefault("pending_items", {})
        item = cart.get(item_id) or pending.get(item_id)

        if not item:
            await query.message.reply_text(
                "I couldn't find this item in your cart - it may have been cleared in the meantime."
            )
            return

        await query.message.reply_text("🔍 Looking for a cheaper alternative...")

        # הכותרת כבר באנגלית (target_language=en) - אין צורך בקריאת API נוספת
        # לשליפת כותרת אנגלית כמו בבוט הישראלי.
        original = ProductRef(
            product_id=item["item_id"],
            title=item["title"],
            price=item["price_usd"],
            image_url=item["image_url"],
            currency=CURRENCY,
            category_id=item.get("category_id"),
            product_url=item["link"],
        )

        try:
            matches, search_stats = await asyncio.to_thread(
                find_cheaper_alternatives, ali_client, original, CURRENCY
            )
        except Exception:
            logger.exception("find_cheaper_alternatives failed for item_id=%s", item_id)
            await query.message.reply_text("Something went wrong with the search. Please try again later.")
            return

        STATS.record_alt_search(search_stats, len(matches))
        await query.message.reply_text(format_alternatives_message(original, matches))

        await _notify_admin(
            context,
            f"🇺🇸 🔍 חיפוש חלופה: {original.title[:60]}\n{format_stats_summary(search_stats)}",
        )

        done.add(item_id)
        try:
            await query.message.edit_reply_markup(
                reply_markup=_current_item_keyboard(context, item_id, query.from_user.id)
            )
        except Exception:
            logger.warning("Could not remove the alt-search button after search for item_id=%s", item_id)
    finally:
        in_progress.discard(item_id)


# --------------------------------------------------------------------------- #
# Main message handler
# --------------------------------------------------------------------------- #
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sender = update.effective_user
    STATS.seen_user(sender)
    if sender:
        context.bot_data.setdefault("user_names", {})[sender.id] = (
            f"@{sender.username}" if sender.username else sender.full_name
        )
    message_text = update.message.text or ""
    seen = set()
    urls = [u for u in ALIEXPRESS_URL_PATTERN.findall(message_text) if not (u in seen or seen.add(u))]

    if not urls:
        STATS.inc("messages_without_link")
        await update.message.reply_text(
            "I didn't find an AliExpress link in your message 🤔 Send me a product link and I'll handle it."
        )
        return

    if len(urls) > MAX_LINKS_PER_MESSAGE:
        await update.message.reply_text(
            f"That's {len(urls)} links in one message - a bit much at once 😅 "
            f"Please send up to {MAX_LINKS_PER_MESSAGE} at a time (you can send "
            f"more in the next message, I remember everything)."
        )
        return

    allowed, retry_after = _check_rate_limit(context, len(urls))
    if not allowed:
        STATS.inc("rate_limited")
        await update.message.reply_text(
            f"Whoa, lots of links in a short time! 😅 You can send up to "
            f"{RATE_LIMIT_MAX_LINKS} links per minute - try again in "
            f"{int(retry_after)} seconds."
        )
        return
    _record_rate_limit(context, len(urls))

    processing_msg = await update.message.reply_text(
        "One sec, checking..." if len(urls) == 1 else f"One sec, checking {len(urls)} items... ⏳"
    )

    STATS.inc("links_received", len(urls))

    new_items = []
    failed_count = 0
    for raw_url in urls:
        try:
            item, seconds = await asyncio.to_thread(_fetch_item_timed, raw_url)
            STATS.record_fetch_time(seconds)
            if item:
                new_items.append(item)
            else:
                failed_count += 1
        except Exception:
            logger.warning("Failed to fetch one item", exc_info=True)
            STATS.inc("fetch_exceptions")
            failed_count += 1

    STATS.inc("items_failed", failed_count)
    for item in new_items:
        STATS.record_product(item, sender)
        if not item["is_affiliate"]:
            STATS.inc("affiliate_fallback")
        if item["catalog_available"]:
            STATS.inc("items_priced")
            STATS.inc("price_sum", item["price_usd"] or 0)
            if item.get("ships_to_us") is False:
                STATS.inc("not_shipping")
            elif best_coupon_for_price(item["price_usd"]):
                STATS.inc("coupon_shown")
            if item.get("ships_to_us") and next_tier_gap(item["price_usd"]):
                STATS.inc("upsell_shown")
        else:
            STATS.inc("items_catalog_less")
            if item.get("foreign_routing_suspected"):
                STATS.inc("foreign_routing")

    # התראת אדמין (בעברית) - מסומנת 🇺🇸 כדי להבדיל מהבוט הישראלי אם
    # שניהם שולחים לאותה קבוצת מעקב.
    admin_entities = []
    prefix = "🇺🇸 👀 "
    if sender and sender.username:
        who = f"@{sender.username}"
    elif sender:
        who = sender.full_name
        admin_entities.append(
            MessageEntity(
                type=MessageEntity.TEXT_MENTION,
                offset=_utf16_len(prefix),
                length=_utf16_len(who),
                user=sender,
            )
        )
    else:
        who = "משתמש לא ידוע"

    admin_lines = [f"{prefix}{who} שלח {len(urls)} קישור/ים:"]
    for item in new_items:
        if item["catalog_available"]:
            admin_lines.append(f"• {item['title']} - {_usd(item['price_usd'])}\n  🔗 {item['raw_url']}")
        else:
            admin_lines.append(f"• (לא נמצאו פרטים מלאים - קישור בלבד)\n  🔗 {item['raw_url']}")
    if failed_count:
        admin_lines.append(f"⚠️ {failed_count} קישורים נכשלו לגמרי")
    await _notify_admin(context, "\n".join(admin_lines), entities=admin_entities or None)

    if not new_items:
        await processing_msg.edit_text(
            "I couldn't pull details for any of those items 😕 "
            "Try sending the full link from the product page itself."
        )
        return

    priced_items = [item for item in new_items if item["catalog_available"]]
    catalog_less_items = [item for item in new_items if not item["catalog_available"]]

    cart = _get_cart(context)

    # מוצר בודד -> "בהמתנה" עד לחיצה על Add to cart. כמה מוצרים -> מיזוג אוטומטי.
    if len(new_items) == 1 and priced_items:
        pending = context.user_data.setdefault("pending_items", {})
        pending[priced_items[0]["item_id"]] = priced_items[0]
        _prune_cart(pending)
    else:
        for item in priced_items:
            cart[item["item_id"]] = item
        _prune_cart(cart)

    coupon_block = _format_coupon_block(cart)
    cart_summary = _format_cart_summary(cart)

    if len(new_items) == 1 and catalog_less_items:
        item = catalog_less_items[0]
        await processing_msg.edit_text(
            "I couldn't automatically pull full details for this item "
            "(price/rating/image), so I don't have a discount code to match. "
            f"Here's your product link:\n{item['link']}"
        )
        return

    if len(new_items) == 1:
        item = new_items[0]
        STATS.inc("single_item_cards")
        item_keyboard = _build_item_keyboard(item["item_id"])
        text = f"🛒 {item['title']}\n\n💰 Price: {_usd(item['price_usd'])}\n"
        try:
            original_differs = float(item["original_price"]) != item["price_usd"]
        except (TypeError, ValueError):
            original_differs = False
        if item["discount"] and original_differs:
            text += f"🔥 Discount: {item['discount']} (was {_usd(item['original_price'])})\n"
        if item["rating"]:
            text += f"⭐ Rating: {item['rating']}\n"
        if not item["ships_to_us"]:
            text += (
                "\n⚠️ I couldn't confirm this item ships to the US - please check "
                "on the product page before buying. That's also why I didn't "
                "include a discount code (the price may not be accurate for the US).\n"
            )
        else:
            solo_hint = _format_solo_coupon_hint(item["price_usd"])
            if solo_hint:
                text += f"\n{solo_hint}\n"
        text += f"\n🔗 Product link:\n{item['link']}\n"
        text += f"\nℹ️ {PRICE_DISCLAIMER}\n"
        text += "\nTap 'Add to cart' 🛒 to include this item in your combined discount total."
        if cart:
            text += f"\n\n{cart_summary}"

        TELEGRAM_CAPTION_LIMIT = 1024
        sent_ok = False
        if item["image_url"]:
            try:
                if len(text) <= TELEGRAM_CAPTION_LIMIT:
                    await update.message.reply_photo(
                        photo=item["image_url"], caption=text, reply_markup=item_keyboard
                    )
                else:
                    await update.message.reply_photo(photo=item["image_url"])
                    await update.message.reply_text(text, reply_markup=item_keyboard)
                sent_ok = True
            except Exception:
                logger.exception("Failed to send photo, falling back to text")

        if sent_ok:
            try:
                await processing_msg.delete()
            except Exception:
                pass
        else:
            try:
                await processing_msg.edit_text(text, reply_markup=item_keyboard)
            except Exception:
                logger.exception("Failed to edit processing message, sending a new one instead")
                await update.message.reply_text(text, reply_markup=item_keyboard)
    else:
        lines = [f"🛒 Added {len(new_items)} new items:\n"]
        for i, item in enumerate(priced_items, start=1):
            ship_note = "" if item["ships_to_us"] else " ⚠️ (US shipping not confirmed)"
            lines.append(f"{i}. {item['title']}\n   💰 {_usd(item['price_usd'])}{ship_note}")
        if coupon_block:
            lines.append("\n" + coupon_block)
        if priced_items:
            lines.append("\n🔗 Links for the new items:")
            for i, item in enumerate(priced_items, start=1):
                lines.append(f"{i}. {item['link']}")
        lines.append(f"\nℹ️ {PRICE_DISCLAIMER}")
        lines.append("\n" + cart_summary)

        if catalog_less_items:
            lines.append(
                f"\nℹ️ I couldn't pull full details for {len(catalog_less_items)} of the "
                "items you sent (so no discount code for those), but here are your links:"
            )
            for i, item in enumerate(catalog_less_items, start=1):
                lines.append(f"{i}. {item['link']}")

        if failed_count:
            lines.append(f"\n(⚠️ {failed_count} link(s) couldn't be recognized and were skipped)")

        try:
            await processing_msg.edit_text("\n".join(lines), reply_markup=CLEAR_CART_KEYBOARD)
        except Exception:
            logger.exception("Failed to edit processing message (multi-item), sending a new one instead")
            await update.message.reply_text("\n".join(lines), reply_markup=CLEAR_CART_KEYBOARD)


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(CONCURRENT_UPDATES).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cart", cart_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(clear_cart_callback, pattern=f"^{CLEAR_CART_CALLBACK}$"))
    application.add_handler(CallbackQueryHandler(add_to_cart_callback, pattern=f"^{ADD_TO_CART_CALLBACK_PREFIX}"))
    application.add_handler(CallbackQueryHandler(find_alternative_callback, pattern=f"^{FIND_ALT_CALLBACK_PREFIX}"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Snagly bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
