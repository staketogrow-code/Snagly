"""
alternative_finder.py
======================
תוסף ל-Snagly: איתור "מוצרים דומים" זולים יותר ממוכרים אחרים באותה קטגוריה.

עקרון העבודה:
1. מחלצים מילות מפתח מכותרת המוצר המקורי (לא כל הכותרת - היא בד"כ עמוסה במילות פילר/SEO)
2. מריצים חיפוש דרך aliexpress.affiliate.product.query עם המילים + category_id + מיון לפי מחיר עולה
3. מסננים את המועמדים לפי:
   - דמיון תמונה (perceptual hash - imagehash) מול תמונת המוצר המקורי
   - דמיון כותרת (Jaccard פשוט על סט המילים)
   - פער מחיר משמעותי (לא רעש של כמה אחוזים)
4. מחזירים רק מועמדים שעברו את שני הסינונים + (בהמשך) עברו את בדיקת המשלוח לישראל הקיימת אצלך

הערות אינטגרציה:
- מעודכן לעבוד מול aliexpress_client.py האמיתי: search_candidates מקבל
  AliExpressClient ומשתמש ב-client.search_products (aliexpress.affiliate.product.query).
- תיקון מטבע: get_product_details ב-aliexpress_client.py משתמש כברירת מחדל ב-ILS,
  אז שדה המחיר כאן נקרא price (לא price_usd כמו בגרסה הקודמת) והוא ב-₪ כברירת מחדל,
  כדי שההשוואה בין המוצר המקורי למועמדים תהיה תמיד באותו מטבע. אם אצלכם
  ProductRef.price נבנה במטבע אחר - צריך להתאים גם את target_currency ב-search_candidates.
- עדיין נשאר TODO אחד: לחבר את בדיקת "מאומת למשלוח לישראל" הקיימת אצלכם
  (מסומן בבירור בתוך find_cheaper_alternatives).
- תלויות חדשות: Pillow, imagehash
    pip install Pillow imagehash
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests
from PIL import Image
import imagehash

from aliexpress_client import AliExpressClient, AliExpressAPIError
# from bot import is_verified_for_israel_shipping  # אם קיימת פונקציה כזו כבר - נחבר בשלב הבא

logger = logging.getLogger("snagly.alternative_finder")

# מילות פילר נפוצות בכותרות AliExpress שכדאי לזרוק לפני חיפוש
STOPWORDS = {
    "new", "hot", "sale", "free", "shipping", "high", "quality", "for",
    "with", "and", "the", "of", "in", "on", "1pcs", "2pcs", "pcs", "set",
    "top", "best", "2023", "2024", "2025", "2026",
    # בולשיט שיווקי/SEO שחוזר על עצמו בהמון כותרות ולא מזהה את המוצר בפועל -
    # כלל כשכתוב "Factory Price" זה לא אומר כלום על מה המוצר, רק "מחיר טוב"
    "factory", "price", "wholesale", "wuzhou", "custom", "customized",
    "fashion", "luxury", "hip", "hop",
    # מילים נרדפות ל"טבעת"/"שרשרת" בשפות אחרות שמופיעות בכותרות רב-לשוניות
    # (טריק SEO של AliExpress) - הן לא שגויות מבחינה סמנטית, אבל הצירוף שלהן
    # עם המילה האנגלית המקבילה מצמצם חיפוש למעט מאוד מוכרים שכתבו בדיוק
    # אותו שילוב שפות, במקום למצוא מוצרים דומים אמיתיים. "ring" עצמה נשארת -
    # היא המילה האנגלית המתארת בפועל את סוג המוצר, לא פילר.
    "anillos", "anillo", "bague", "bagues", "anello", "bijoux",
}

# סף רגישות ל-perceptual hash (0 = זהה לחלוטין, ~64 = שונה לגמרי ב-phash סטנדרטי)
# עודכן מ-10 ל-40 על סמך נתונים אמיתיים: מוצרי ROJECO קשורים בפועל (אותה
# משפחת מוצרים, ממוכרים/רישומים שונים) נמדדו במרחק 24-34 בגלל הבדלי זווית
# צילום/רקע/watermark - סף של 10 היה קשיח מדי ופסל התאמות אמיתיות.
IMAGE_HASH_MAX_DISTANCE = 40

# סף דמיון כותרת (Jaccard, 0-1)
TITLE_SIMILARITY_MIN = 0.35

# כמה זול צריך להיות המועמד לעומת המקור כדי שיהיה שווה להציג (5% ומעלה)
MIN_PRICE_DROP_RATIO = 0.05

# תקרת סבירות: אם מועמד "זול" יותר מזה, כנראה שזה לא באמת אותו מוצר -
# למשל אביזר/חלק חילוף (מסנן, משאבה) של אותה משפחת מוצרים, לא היחידה השלמה.
# מוצרים אמיתיים מאותה קטגוריה בד"כ לא נבדלים במחיר יותר מזה בין מוכרים.
MAX_PRICE_DROP_RATIO = 0.60

# מילות מפתח שמעידות שהמועמד הוא אביזר/חלק חילוף ולא המוצר השלם - גם אם
# הכותרת והתמונה דומות מאוד (משפחת מוצרים זהה), אלה לא תחליף אמיתי.
ACCESSORY_KEYWORDS = {
    "filter", "filters", "pump", "pumps", "replacement", "spare",
    "accessory", "accessories", "part", "parts", "kit", "cartridge",
}

# כמה מועמדים לבקש מה-API לפני סינון (חלק ייפלו בסינון)
CANDIDATE_POOL_SIZE = 20

# כמה תוצאות סופיות להחזיר למשתמש לכל היותר
MAX_RESULTS = 3


@dataclass
class ProductRef:
    """
    ייצוג מינימלי של מוצר, כמו שכבר יש לך במודל הפנימי של הבוט.

    currency: קוד המטבע שבו price מבוטא ("USD" או "ILS"). ב-bot.py כבר יש
    גם price_ils וגם price_usd מוכנים - מומלץ USD כאן כי coupons.py כבר
    עובד בדולר, כך שהכל באותה שפת מספרים.
    """
    product_id: str
    title: str
    price: float
    image_url: str
    currency: str = "USD"
    category_id: Optional[str] = None
    product_url: Optional[str] = None


@dataclass
class AlternativeMatch:
    product: ProductRef
    image_distance: int
    title_similarity: float
    price_drop_ratio: float


def extract_keywords(title: str, max_words: int = 5) -> str:
    """
    מחלץ 3-5 מילות מפתח משמעותיות מתוך כותרת ארוכה.

    מגביל את הסריקה ל-10 המילים הראשונות של הכותרת בלבד: בכותרות AliExpress
    עמוסות-SEO, התיאור האמיתי של המוצר כמעט תמיד בהתחלה, וה"תיוג" הרב-לשוני
    (anillos/bague/homme/pareja וכו') מתווסף בסוף. הגבלת חלון הסריקה נמנעת
    מהצורך לחסום כל שפה אפשרית בנפרד ברשימת STOPWORDS.
    """
    title_prefix = " ".join(title.split()[:10])
    words = re.findall(r"[A-Za-z\u0590-\u05FF]+", title_prefix.lower())
    words = [w for w in words if w not in STOPWORDS and len(w) > 2]
    # שומר על סדר הופעה, מסיר כפילויות
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return " ".join(seen[:max_words])


def _title_token_set(title: str) -> set[str]:
    words = re.findall(r"[A-Za-z\u0590-\u05FF]+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def title_similarity(title_a: str, title_b: str) -> float:
    """דמיון Jaccard בין שתי כותרות - סינון עזר בלבד, לא הקריטריון העיקרי."""
    set_a, set_b = _title_token_set(title_a), _title_token_set(title_b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def is_accessory_listing(title: str) -> bool:
    """מזהה כותרות שמעידות על אביזר/חלק חילוף ולא המוצר השלם (פילטר, משאבה וכו')."""
    tokens = _title_token_set(title)
    return bool(tokens & ACCESSORY_KEYWORDS)


def _download_image(url: str, timeout: int = 6) -> Optional[Image.Image]:
    try:
        # AliExpress חוסם User-Agent גנרי - כמו שכבר טיפלתם בפתרון קישורים מקוצרים
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        logger.warning("נכשל בהורדת תמונה מ-%s: %s", url, e)
        return None


def image_distance(image_url_a: str, image_url_b: str) -> Optional[int]:
    """מרחק perceptual hash בין שתי תמונות מוצר. None אם אחת מהן לא נטענה."""
    img_a = _download_image(image_url_a)
    img_b = _download_image(image_url_b)
    if img_a is None or img_b is None:
        return None
    hash_a = imagehash.phash(img_a)
    hash_b = imagehash.phash(img_b)
    return hash_a - hash_b  # hamming distance, imagehash תומך ב-subtraction ישירות


def search_candidates(
    client: AliExpressClient,
    keywords: str,
    category_id: Optional[str],
    target_currency: str = "USD",
) -> list[ProductRef]:
    """
    קורא ל-client.search_products (aliexpress.affiliate.product.query),
    ממוין לפי מחיר עולה, וממפה את התוצאות הגולמיות ל-ProductRef.

    target_currency: חייב להתאים למטבע שבו נשלף מחירו של original ב-find_cheaper_alternatives,
    אחרת ההשוואה בין המחירים לא תקפה (ILS מול USD למשל).
    """
    try:
        raw_products = client.search_products(
            keywords=keywords,
            category_id=category_id,
            sort="SALE_PRICE_ASC",
            page_size=CANDIDATE_POOL_SIZE,
            target_currency=target_currency,
            ship_to_country="US",
        )
    except AliExpressAPIError as e:
        logger.warning("search_products נכשל עבור מילות מפתח '%s': %s", keywords, e)
        return []

    logger.info(
        "search_candidates: keywords='%s' category_id=%s -> %d תוצאות גולמיות מה-API",
        keywords, category_id, len(raw_products),
    )

    candidates: list[ProductRef] = []
    for item in raw_products:
        try:
            candidates.append(ProductRef(
                product_id=str(item["product_id"]),
                title=item["product_title"],
                price=float(item["target_sale_price"]),
                image_url=item["product_main_image_url"],
                currency=target_currency,
                category_id=item.get("first_level_category_id"),
                product_url=item.get("product_detail_url"),
            ))
        except (KeyError, ValueError, TypeError) as e:
            # מוצר בודד עם שדות חסרים/פגומים לא אמור להפיל את כל החיפוש
            logger.debug("דילוג על מועמד עם שדות חסרים: %s (%s)", item.get("product_id"), e)
            continue

    logger.info("search_candidates: %d/%d מועמדים מופו בהצלחה ל-ProductRef", len(candidates), len(raw_products))

    return candidates


def find_cheaper_alternatives(
    client: AliExpressClient,
    original: ProductRef,
    target_currency: str = "USD",
) -> tuple[list[AlternativeMatch], dict]:
    """
    הפונקציה המרכזית: מחפשת מועמדים, מסננת לפי תמונה+כותרת+מחיר,
    ומחזירה עד MAX_RESULTS התאמות ממוינות מהזול ביותר.

    target_currency חייב להתאים למטבע שבו original.price כבר נשלף
    (ברירת המחדל USD תואמת ל-price_usd שכבר קיים ב-bot.py).

    מחזירה tuple (matches, stats) - stats הוא dict עם ספירה של כמה מועמדים
    נפסלו על כל סיבה (מחיר/כותרת/אביזר/תמונה) וכמה עברו. זו שכבת המדידה
    שמאפשרת להחליט, על סמך נתונים אמיתיים מהלוגים/מעקב האדמין, אם באמת
    שווה להשקיע בשדרוג image_distance (embedding) או שהצוואר-בקבוק
    האמיתי הוא עדיין בשלב הטקסטואלי (מחיר/כותרת/חיפוש).
    """
    stats = {
        "raw_candidates": 0,
        "rejected_price": 0,
        "rejected_accessory": 0,
        "rejected_title": 0,
        "rejected_image": 0,
        "passed": 0,
    }

    keywords = extract_keywords(original.title)
    if not keywords:
        logger.info("לא נמצאו מילות מפתח שימושיות בכותרת: %s", original.title)
        return [], stats

    candidates = search_candidates(client, keywords, original.category_id, target_currency)
    stats["raw_candidates"] = len(candidates)

    matches: list[AlternativeMatch] = []

    for candidate in candidates:
        if candidate.product_id == original.product_id:
            continue  # אותו מוצר בדיוק, לא מעניין

        price_drop_ratio = (original.price - candidate.price) / original.price
        if price_drop_ratio < MIN_PRICE_DROP_RATIO:
            logger.info(
                "מועמד %s נפסל: זול רק ב-%.1f%% (סף: %.0f%%) - '%s'",
                candidate.product_id, price_drop_ratio * 100, MIN_PRICE_DROP_RATIO * 100, candidate.title[:50],
            )
            stats["rejected_price"] += 1
            continue  # לא זול מספיק כדי להיות שווה טרחה

        if price_drop_ratio > MAX_PRICE_DROP_RATIO:
            logger.info(
                "מועמד %s נפסל: זול ב-%.1f%% - חשוד מדי, כנראה לא אותו מוצר (סף עליון: %.0f%%) - '%s'",
                candidate.product_id, price_drop_ratio * 100, MAX_PRICE_DROP_RATIO * 100, candidate.title[:50],
            )
            stats["rejected_price"] += 1
            continue  # זול בצורה לא סבירה - כנראה אביזר/חלק ולא היחידה השלמה

        if is_accessory_listing(candidate.title):
            logger.info(
                "מועמד %s נפסל: הכותרת מעידה על אביזר/חלק חילוף - '%s'",
                candidate.product_id, candidate.title[:50],
            )
            stats["rejected_accessory"] += 1
            continue  # פילטר/משאבה/חלק חילוף - לא המוצר השלם

        sim = title_similarity(original.title, candidate.title)
        if sim < TITLE_SIMILARITY_MIN:
            logger.info(
                "מועמד %s נפסל: דמיון כותרת %.2f (סף: %.2f) - '%s'",
                candidate.product_id, sim, TITLE_SIMILARITY_MIN, candidate.title[:50],
            )
            stats["rejected_title"] += 1
            continue  # כותרות שונות מדי - כנראה לא אותו מוצר

        dist = image_distance(original.image_url, candidate.image_url)
        if dist is None or dist > IMAGE_HASH_MAX_DISTANCE:
            logger.info(
                "מועמד %s נפסל: מרחק תמונה %s (סף: %d) - '%s'",
                candidate.product_id, dist, IMAGE_HASH_MAX_DISTANCE, candidate.title[:50],
            )
            stats["rejected_image"] += 1
            continue  # תמונה לא זמינה או שונה מדי ויזואלית

        logger.info(
            "מועמד %s עבר: מחיר -%.1f%%, דמיון כותרת %.2f, מרחק תמונה %s",
            candidate.product_id, price_drop_ratio * 100, sim, dist,
        )
        stats["passed"] += 1

        # TODO: לשלב כאן קריאה לבדיקת המשלוח לישראל הקיימת אצלכם, לפני שמוסיפים
        # להתאמות, כדי לא להציע אלטרנטיבה שלא ניתנת להזמנה בפועל:
        # if not is_verified_for_israel_shipping(candidate.product_id):
        #     continue

        matches.append(AlternativeMatch(
            product=candidate,
            image_distance=dist,
            title_similarity=sim,
            price_drop_ratio=price_drop_ratio,
        ))

    # מיון: קודם הכי דומה ויזואלית, ואז הכי זול
    matches.sort(key=lambda m: (m.image_distance, -m.price_drop_ratio))
    top_matches = matches[:MAX_RESULTS]

    # חשוב: הלינק שחוזר מ-search_products הוא לינק גולמי, לא לינק אפיליאייט -
    # בלי ההמרה הזו לא הייתה נוצרת עמלה על קליקים על החלופות. ממירים רק את
    # ה-MAX_RESULTS הסופיים (לא את כל המועמדים) כדי לחסוך קריאות API מיותרות.
    for m in top_matches:
        if not m.product.product_url:
            continue
        try:
            m.product.product_url = client.generate_affiliate_link(m.product.product_url)
        except AliExpressAPIError:
            logger.warning(
                "לא ניתן היה ליצור לינק אפיליאייט לחלופה %s - נשאר הלינק המקורי (בלי עמלה)",
                m.product.product_id,
            )

    return top_matches, stats


def format_stats_summary(stats: dict) -> str:
    """מנסח שורת סטטיסטיקה קצרה לצורך מעקב אדמין - לא מוצג למשתמש הסופי."""
    return (
        f"📊 {stats['raw_candidates']} מועמדים גולמיים | "
        f"עברו: {stats['passed']} | "
        f"נפסלו - מחיר: {stats['rejected_price']}, "
        f"אביזר: {stats['rejected_accessory']}, "
        f"כותרת: {stats['rejected_title']}, "
        f"תמונה: {stats['rejected_image']}"
    )


def format_alternatives_message(original: ProductRef, matches: list[AlternativeMatch]) -> str:
    """מנסח את הודעת התשובה למשתמש, בהתאם לעקרון השקיפות של Snagly."""
    if not matches:
        return (
            "🔍 No alternative met our bar this time - we only show items that "
            "are at least 5% cheaper and genuinely look like the same product. "
            "The price you already found may simply be the best one right now."
        )

    symbol = {"USD": "$", "ILS": "₪"}.get(original.currency, original.currency + " ")

    lines = ["🔍 Found some very similar-looking items at a lower price:\n"]
    for i, m in enumerate(matches, start=1):
        pct = round(m.price_drop_ratio * 100)
        lines.append(
            f"{i}. {m.product.title[:60]}...\n"
            f"   💰 {symbol}{m.product.price:.2f} (~{pct}% less)\n"
            f"   🔗 {m.product.product_url or '(link unavailable)'}\n"
        )
    lines.append(
        "\n⚠️ Note: these are similar items from different sellers, not "
        "guaranteed to be 100% identical (quality/version/variant). Please "
        "check the product page before buying."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ה-handler האמיתי (find_alternative_callback) נמצא ב-bot.py, לא כאן -
# ראו את הפאץ' שסופק בצ'אט לחיבור הכפתור וה-handler.
# ---------------------------------------------------------------------------
