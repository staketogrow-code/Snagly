"""
aliexpress_client.py

Minimal client for the AliExpress Open Platform Affiliate API.
Implements request signing (HMAC-SHA256) and three calls:
  - aliexpress.affiliate.productdetail.get  -> product info by ID
  - aliexpress.affiliate.link.generate      -> convert a plain link into
                                                an affiliate tracking link
  - aliexpress.affiliate.product.query      -> keyword search, used to find
                                                cheaper look-alike products
                                                (see alternative_finder.py)

Docs: https://openservice.aliexpress.com/doc/doc.htm
"""

import hashlib
import hmac
import re
import threading
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_GATEWAY = "https://api-sg.aliexpress.com/sync"

# מניעה יזומה של ApiCallLimit (ר' _call): מרווח מינימלי בין קריאות API
# עוקבות, לא רק retry אחרי שכבר נחסמנו.
#
# 0.5 שניות היה ערך התחלתי - התברר (21.9.2026) שקטן מדי: כל _fetch_item
# בודד כבר עושה 2 קריאות רצופות ל-productdetail.get (שקל ואז דולר), ועם
# רק 0.5 שנייה ביניהן, גם שימוש חד-פעמי ורגיל - בלי שום ניסיון חוזר של
# המשתמש - יכול להיתקל בהגבלה כבר על הקריאה השנייה, כי AliExpress עצמם
# מדווחים על חסימה שנמשכת "שנייה אחת" (>0.5). הועלה ל-1.2 שניות - מעל
# מה שה-API מדווח כמשך החסימה בפועל - כדי שגם שתי הקריאות הרצופות
# באותה שליפה יתפזרו מספיק ולא ידרשו retry בכלל במקרה הרגיל.
MIN_CALL_INTERVAL_SECONDS = 1.2

# Cache (22.9.2026): מוצרים פופולריים נשלחים שוב ושוב ע"י משתמשים שונים -
# כל פגיעה ב-cache חוסכת קריאת API ומורידה את הלחץ על ApiCallLimit.
# גם תוצאה ריקה (מוצר שלא בקטלוג) נשמרת, לזמן קצר יותר - כך ששליחה חוזרת
# ומהירה של קישור שנכשל לא מייצרת שוב סערת קריאות (ר' learnings על retry storms).
PRODUCT_CACHE_TTL_SECONDS = 30 * 60
PRODUCT_NEGATIVE_CACHE_TTL_SECONDS = 10 * 60
# קישור אפיליאייט למוצר יציב לאורך זמן - אפשר לשמור יותר.
LINK_CACHE_TTL_SECONDS = 24 * 60 * 60
LINK_NEGATIVE_CACHE_TTL_SECONDS = 60 * 60
MAX_CACHE_ENTRIES = 5000

_NOT_ELIGIBLE = object()  # sentinel: מוצר שאינו זכאי לקישור אפיליאייט


class AliExpressAPIError(Exception):
    """Raised when the AliExpress API returns an error response."""


class AliExpressClient:
    def __init__(self, app_key: str, app_secret: str, tracking_id: str, proxy_url: Optional[str] = None):
        self.app_key = app_key
        self.app_secret = app_secret
        self.tracking_id = tracking_id
        # אופציונלי: proxy עם IP אמריקאי. בגרסה האמריקאית בד"כ לא נדרש -
        # AliExpress מנתב לפי IP של השרת, ושרת Railway באזור US כבר מקבל
        # ניתוב אמריקאי כברירת מחדל. רלוונטי רק אם השרת רץ באזור לא-אמריקאי.
        # אם None - בלי proxy.
        self.proxy_url = proxy_url
        # למניעה יזומה של ApiCallLimit (ר' _call) - זמן הקריאה האחרונה
        # ל-API, לפי מונה שעון יציב (לא מושפע משעון מערכת שמשתנה).
        self._last_call_time = 0.0

        # Thread safety (22.9.2026): הבוט מריץ עכשיו שליפות במקביל ב-threads
        # (asyncio.to_thread), אז ה-throttle חייב נעילה - אחרת שני threads
        # יכולים לקרוא את _last_call_time באותו רגע ולירות יחד.
        self._throttle_lock = threading.Lock()
        # אחרי ApiCallLimit - כל ה-threads (לא רק זה שנחסם) ממתינים עד לכאן,
        # כדי שקריאות מקבילות של משתמשים אחרים לא ימשיכו להלום בחסימה.
        self._blocked_until = 0.0

        self._cache_lock = threading.Lock()
        self._product_cache = {}
        self._link_cache = {}

        self._stats_lock = threading.Lock()
        self.api_stats = {
            "calls": 0,
            "call_limit_hits": 0,
            "retries_exhausted": 0,
            "api_errors": 0,
            "product_cache_hits": 0,
            "product_cache_misses": 0,
            "link_cache_hits": 0,
            "link_cache_misses": 0,
        }
        self.api_calls_by_method = {}

    # ------------------------------------------------------------------ #
    # Stats / cache / throttle helpers
    # ------------------------------------------------------------------ #
    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.api_stats[key] = self.api_stats.get(key, 0) + n

    def get_api_stats(self) -> dict:
        with self._stats_lock:
            data = dict(self.api_stats)
            data["by_method"] = dict(self.api_calls_by_method)
        with self._cache_lock:
            data["product_cache_size"] = len(self._product_cache)
            data["link_cache_size"] = len(self._link_cache)
        return data

    def _cache_get(self, cache: dict, key):
        """מחזיר (found, value). מוחק רשומה שפג תוקפה."""
        with self._cache_lock:
            entry = cache.get(key)
            if not entry:
                return False, None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del cache[key]
                return False, None
            return True, value

    def _cache_set(self, cache: dict, key, value, ttl: float) -> None:
        with self._cache_lock:
            if len(cache) >= MAX_CACHE_ENTRIES:
                now = time.monotonic()
                for k in [k for k, (exp, _) in cache.items() if exp < now]:
                    del cache[k]
                if len(cache) >= MAX_CACHE_ENTRIES:
                    # עדיין מלא - מוחקים את הרבע הישן ביותר (לפי זמן תפוגה)
                    oldest = sorted(cache.items(), key=lambda kv: kv[1][0])[: MAX_CACHE_ENTRIES // 4]
                    for k, _ in oldest:
                        del cache[k]
            cache[key] = (time.monotonic() + ttl, value)

    def _throttle(self) -> None:
        """
        מרווח מינימלי גלובלי בין קריאות API, בטוח ל-threads. הנעילה מוחזקת
        גם בזמן ההמתנה - זה מכוון: כך הקריאות מכל ה-threads יוצאות בטור
        מסודר עם מרווח, במקום שכולם יתעוררו יחד וירו באותו רגע.
        """
        with self._throttle_lock:
            now = time.monotonic()
            wait_until = max(self._last_call_time + MIN_CALL_INTERVAL_SECONDS, self._blocked_until)
            if wait_until > now:
                time.sleep(wait_until - now)
            self._last_call_time = time.monotonic()

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    def _sign(self, params: dict) -> str:
        """
        AliExpress / TOP-protocol signing (HMAC-SHA256):
          1. Sort all params (excluding 'sign') by key.
          2. Concatenate as key1value1key2value2...
          3. HMAC-SHA256 with app_secret as the key, uppercase hex digest.
        """
        sorted_items = sorted(params.items())
        base_string = "".join(f"{k}{v}" for k, v in sorted_items)
        signature = hmac.new(
            self.app_secret.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()
        return signature

    def _call(self, method: str, business_params: dict, max_retries: int = 6) -> dict:
        """
        Build a signed request, call the gateway, and return the parsed result.

        Throttles proactively (see MIN_CALL_INTERVAL_SECONDS) before every
        outbound request - added 21.9.2026 after a real production case
        where all 6 retries were exhausted, twice, a few minutes apart, on
        the exact same item. That pattern (consistent failure across
        multiple spaced-out retries, not just occasional bad luck) pointed
        to sustained contention from overall call volume rather than a
        one-off collision - so waiting reactively after getting rate-limited
        wasn't enough; better to avoid firing calls too close together in
        the first place.

        Also retries automatically (up to max_retries times) on ApiCallLimit
        errors as a safety net - AliExpress's short-lived rate limiting
        (typically a 1-second ban) that normally clears itself almost
        immediately. max_retries raised from 3 to 6 on 21.9.2026 for the
        same reason as above.
        """
        last_error: Optional["AliExpressAPIError"] = None

        for attempt in range(max_retries):
            self._throttle()
            self._bump("calls")
            with self._stats_lock:
                self.api_calls_by_method[method] = self.api_calls_by_method.get(method, 0) + 1

            params = {
                "app_key": self.app_key,
                "method": method,
                "sign_method": "sha256",
                "timestamp": str(int(time.time() * 1000)),
                "v": "2.0",
                "format": "json",
            }
            params.update({k: v for k, v in business_params.items() if v is not None})
            params["sign"] = self._sign(params)

            response = requests.get(API_GATEWAY, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            # AliExpress wraps errors as {"error_response": {...}}
            if "error_response" in data:
                err = data["error_response"]
                code = err.get("code", "")
                message = f"{code}: {err.get('msg')} ({err.get('sub_msg', '')})"

                if code == "ApiCallLimit" and attempt < max_retries - 1:
                    # Parse "this ban will last N seconds" if present, otherwise
                    # fall back to a short default wait. Multiply by (attempt+1)
                    # as a progressive backoff - added 21.9.2026 after a real
                    # case where fixed ~1.2s waits kept re-hitting the SAME
                    # still-active limit on every retry, all 6 attempts
                    # failing, twice in a row. AliExpress's reported "1
                    # second" ban duration doesn't seem fully reliable under
                    # sustained load, so later retries wait longer instead of
                    # repeating the same short gap indefinitely.
                    wait_match = re.search(r"last (\d+(?:\.\d+)?) second", err.get("msg", ""))
                    base_wait = float(wait_match.group(1)) if wait_match else 1.0
                    wait_seconds = base_wait * (attempt + 1)
                    logger.info(
                        "ApiCallLimit hit for %s - retrying in %.1fs (attempt %d/%d)",
                        method, wait_seconds, attempt + 1, max_retries,
                    )
                    self._bump("call_limit_hits")
                    # לא ישנים כאן ישירות - מזיזים את _blocked_until קדימה, ו-_throttle
                    # בסיבוב הבא (וגם של כל thread אחר) ימתין עד אז.
                    with self._throttle_lock:
                        self._blocked_until = max(
                            self._blocked_until, time.monotonic() + wait_seconds + 0.2
                        )
                    last_error = AliExpressAPIError(message)
                    continue

                if code == "ApiCallLimit":
                    self._bump("call_limit_hits")
                    self._bump("retries_exhausted")
                else:
                    self._bump("api_errors")
                raise AliExpressAPIError(message)

            return data

        # Exhausted retries, all of them ApiCallLimit
        self._bump("retries_exhausted")
        raise last_error

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_item_id(url: str) -> Optional[str]:
        """
        Extract the numeric AliExpress product/item ID from a variety of
        URL shapes, e.g.:
          https://www.aliexpress.com/item/1005006123456789.html
          https://he.aliexpress.com/item/1005006123456789.html?spm=...
        Returns None if no ID could be found (e.g. it's a shortened
        s.click.aliexpress.com link that still needs to be resolved first).
        """
        match = re.search(r"/item/(\d+)\.html", url)
        if match:
            return match.group(1)
        return None

    def resolve_short_link(self, url: str) -> str:
        """
        Follows redirects for shortened AliExpress links
        (a.aliexpress.com/..., s.click.aliexpress.com, he.aliexpress.com/e/...
        etc.) and returns the final, full item URL.

        AliExpress blocks obvious non-browser requests (per hard-won
        experience) - a plain requests.get with no headers often gets
        redirected to a bot-check or a generic page instead of the real
        item, silently breaking resolution. A realistic browser User-Agent
        (and GET instead of HEAD, which is blocked more aggressively)
        avoids most of that.

        US VERSION NOTE: for the US bot, a US-region server is already the
        "right" geo, so routing to aliexpress.us / glo2usa is expected and
        fine. The history below is from the Israeli bot.

        GEO-ROUTING FIX (30.8.2026): confirmed 25.8.2026 that since the bot
        runs on a server outside Israel, AliExpress routes short links to a
        region-specific item_id (observed: a US variant, gatewayAdapt=
        glo2usa4itemAdapt) that isn't in our Israel-scoped affiliate
        catalog - appending gatewayAdapt=glo2isr as a hint had zero effect
        (AliExpress decides purely by server IP geolocation). Fixed instead
        by routing this specific request through an Israel-geolocated
        residential proxy (self.proxy_url, e.g. DataImpulse) when
        configured - confirmed via ip-api.com that it returns a real
        Tel-Aviv-based Israeli IP. Falls back to a direct (non-proxied)
        request if no proxy is configured, so this works unmodified for
        anyone who hasn't set one up.

        Uses stream=True so we don't download the full final page body
        (often the biggest part of the response) - we only need
        response.url, not the page content, so this saves bandwidth/proxy
        traffic without changing behavior.
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        try:
            response = requests.get(
                url, allow_redirects=True, timeout=15, headers=headers, proxies=proxies, stream=True
            )
            final_url = response.url
            status_code = response.status_code
            response.close()  # לא צריך את גוף הדף - חוסך רוחב פס/תעבורת proxy
            logger.info(
                "resolve_short_link: %s -> %s (status %s, proxy=%s)",
                url, final_url, status_code, bool(self.proxy_url),
            )
            return final_url
        except requests.RequestException:
            logger.warning("resolve_short_link failed for %s", url, exc_info=True)
            return url

    # ------------------------------------------------------------------ #
    # API calls
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_result(response_wrapper: dict) -> dict:
        """
        AliExpress sometimes wraps the payload as {"resp_result": {"result": {...}}}
        and sometimes returns {"result": {...}} directly, depending on the
        method/account. Try both shapes before giving up.
        """
        if "resp_result" in response_wrapper:
            return response_wrapper["resp_result"].get("result", {})
        if "result" in response_wrapper:
            return response_wrapper["result"]
        return response_wrapper

    def get_product_details(self, item_id: str, target_currency="USD",
                             target_language="en", ship_to_country="US"):
        """
        Calls aliexpress.affiliate.productdetail.get for a single item ID.
        Cached per (item, currency, language, country) - see PRODUCT_CACHE_TTL_SECONDS.
        """
        cache_key = (item_id, target_currency, target_language, ship_to_country)
        found, cached = self._cache_get(self._product_cache, cache_key)
        if found:
            self._bump("product_cache_hits")
            return cached
        self._bump("product_cache_misses")

        product = self._get_product_details_uncached(item_id, target_currency, target_language, ship_to_country)
        ttl = PRODUCT_CACHE_TTL_SECONDS if product else PRODUCT_NEGATIVE_CACHE_TTL_SECONDS
        self._cache_set(self._product_cache, cache_key, product, ttl)
        return product

    def _get_product_details_uncached(self, item_id, target_currency, target_language, ship_to_country):
        data = self._call(
            "aliexpress.affiliate.productdetail.get",
            {
                "product_ids": item_id,
                "target_currency": target_currency,
                "target_language": target_language,
                "ship_to_country": ship_to_country,
                "tracking_id": self.tracking_id,
            },
        )
        try:
            response_wrapper = data["aliexpress_affiliate_productdetail_get_response"]
            result = self._extract_result(response_wrapper)
            products = result.get("products", {}).get("product", [])
            return products[0] if products else None
        except (KeyError, IndexError) as e:
            logger.error("Raw AliExpress response (productdetail.get): %s", data)
            raise AliExpressAPIError(f"Unexpected response shape: {e}") from e

    def generate_affiliate_link(self, source_url: str) -> str:
        """
        Calls aliexpress.affiliate.link.generate and returns the promotion link.

        NOTE (30.8.2026): AliExpress sometimes returns a 200/success response
        where the link object only has a "source_value" field (echoing back
        the input URL) instead of "promotion_link" - this happens when the
        product isn't eligible for the affiliate program (the same
        underlying cause as get_product_details coming back empty for
        "genuinely uncatalogued" items - see resolve_short_link). This is an
        expected outcome, not a bug, so we raise cleanly with a calm INFO log
        instead of letting it fall through to a KeyError + scary traceback -
        the caller already handles this by falling back to the plain link.
        """
        found, cached = self._cache_get(self._link_cache, source_url)
        if found:
            self._bump("link_cache_hits")
            if cached is _NOT_ELIGIBLE:
                raise AliExpressAPIError("Product not eligible for affiliate tracking (cached)")
            return cached
        self._bump("link_cache_misses")

        try:
            link = self._generate_affiliate_link_uncached(source_url)
        except AliExpressAPIError as e:
            if "not eligible" in str(e):
                self._cache_set(self._link_cache, source_url, _NOT_ELIGIBLE, LINK_NEGATIVE_CACHE_TTL_SECONDS)
            raise
        self._cache_set(self._link_cache, source_url, link, LINK_CACHE_TTL_SECONDS)
        return link

    def _generate_affiliate_link_uncached(self, source_url: str) -> str:
        data = self._call(
            "aliexpress.affiliate.link.generate",
            {
                "source_values": source_url,
                "promotion_link_type": "0",
                "tracking_id": self.tracking_id,
            },
        )
        try:
            response_wrapper = data["aliexpress_affiliate_link_generate_response"]
            result = self._extract_result(response_wrapper)
            links = result.get("promotion_links", {}).get("promotion_link", [])
            if not links:
                logger.error("Raw AliExpress response (link.generate, no links): %s", data)
                raise AliExpressAPIError("No promotion link returned")

            link_obj = links[0]
            if "promotion_link" in link_obj:
                return link_obj["promotion_link"]

            logger.info(
                "generate_affiliate_link: no tracking link available for %s "
                "(product likely not eligible for the affiliate program)", source_url,
            )
            raise AliExpressAPIError("Product not eligible for affiliate tracking (source_value only)")
        except (KeyError, IndexError) as e:
            logger.error("Raw AliExpress response (link.generate): %s", data)
            raise AliExpressAPIError(f"Unexpected response shape: {e}") from e

    def search_products(
        self,
        keywords: str,
        category_id: Optional[str] = None,
        sort: str = "SALE_PRICE_ASC",
        page_size: int = 20,
        target_currency: str = "USD",
        target_language: str = "en",
        ship_to_country: str = "US",
    ) -> list:
        """
        Calls aliexpress.affiliate.product.query - keyword search across the
        catalog, optionally scoped to a category, sorted by price.

        Used by alternative_finder.py to look for cheaper look-alike
        products from other sellers. Returns the raw list of product dicts
        as AliExpress returns them (not wrapped in ProductRef - that mapping
        happens in alternative_finder.py).

        NOTE on language: defaults to English. Hebrew keywords were tested
        and the API effectively ignored them, returning unrelated cheap
        products sorted only by price - so keyword extraction/matching in
        alternative_finder.py should be done against an English title.

        NOTE on currency: defaults to USD to match get_product_details above,
        so that price comparisons between the original product and search
        results use the same currency. If you call this with a different
        target_currency, make sure the original product's price was fetched
        with the same currency too.
        """
        data = self._call(
            "aliexpress.affiliate.product.query",
            {
                "keywords": keywords,
                "category_ids": category_id,
                "sort": sort,
                "page_size": page_size,
                "target_currency": target_currency,
                "target_language": target_language,
                "ship_to_country": ship_to_country,
                "tracking_id": self.tracking_id,
            },
        )
        try:
            response_wrapper = data["aliexpress_affiliate_product_query_response"]
            result = self._extract_result(response_wrapper)
            return result.get("products", {}).get("product", [])
        except (KeyError, IndexError) as e:
            logger.error("Raw AliExpress response (product.query): %s", data)
            raise AliExpressAPIError(f"Unexpected response shape: {e}") from e