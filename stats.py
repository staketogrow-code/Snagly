"""
stats.py - מעקב שימוש בזיכרון עבור /stats (אדמין בלבד).

שים לב: כל הנתונים כאן מתאפסים בכל redeploy/restart (כמו העגלה), עד
שיהיה מסד נתונים. ה-/stats מציג תמיד "מאז" כדי שיהיה ברור לאיזה חלון זמן
המספרים מתייחסים.

כל העדכונים נעשים מתוך ה-event loop של הבוט (לא מתוך threads), כך שאין
צורך בנעילות כאן. נתוני ה-API (קריאות, cache, חסימות) נאספים בנפרד בתוך
AliExpressClient ומועברים לכאן רק לצורך התצוגה.
"""

import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone

ISRAEL_TZ = timezone(timedelta(hours=3))  # שעון ישראל (קיץ) - לחלוקת "היום/אתמול"
DAILY_HISTORY_DAYS = 7


def _pct(part, whole) -> str:
    return f"{(part / whole * 100):.0f}%" if whole else "—"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} ימים {hours} שעות"
    if hours:
        return f"{hours} שעות {minutes} דק'"
    return f"{minutes} דק'"


class BotStats:
    def __init__(self, currency_symbol: str = "₪", label: str = ""):
        self.started_at = time.time()
        self.currency_symbol = currency_symbol
        self.label = label

        self.totals = Counter()
        self.daily = defaultdict(Counter)        # "YYYY-MM-DD" -> Counter
        self.daily_users = defaultdict(set)      # "YYYY-MM-DD" -> {user_id}
        self.all_users = set()
        self.start_users = set()                 # שלחו /start מאז ההפעלה
        self.user_days = defaultdict(set)        # user_id -> {תאריכים שבהם היה פעיל}
        self.user_first_day = {}                 # user_id -> התאריך הראשון שראינו אותו

        self.product_counts = Counter()          # item_id -> כמה פעמים נשלח
        self.product_titles = {}                 # item_id -> כותרת
        self.product_users = defaultdict(set)    # item_id -> {user_id}

        self.referral_counts = Counter()         # referrer_id -> משתמשים שהגיעו דרכו
        self.referrer_names = {}
        self.referred_users = set()

        self.fetch_times = deque(maxlen=500)     # שניות לכל שליפת מוצר
        self.alt_rejections = Counter()

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def _today(self) -> str:
        return datetime.now(ISRAEL_TZ).strftime("%Y-%m-%d")

    def _prune_daily(self) -> None:
        cutoff = (datetime.now(ISRAEL_TZ) - timedelta(days=DAILY_HISTORY_DAYS)).strftime("%Y-%m-%d")
        for day in [d for d in self.daily if d < cutoff]:
            del self.daily[day]
        for day in [d for d in self.daily_users if d < cutoff]:
            del self.daily_users[day]

    def inc(self, key: str, n=1) -> None:
        if not n:
            return
        self.totals[key] += n
        self.daily[self._today()][key] += n

    def seen_user(self, user) -> None:
        if not user:
            return
        today = self._today()
        self.all_users.add(user.id)
        self.daily_users[today].add(user.id)
        # שימור: לא מוחקים ימים ישנים כאן (בניגוד ל-daily_users), כי זה בדיוק
        # מה שמאפשר לראות מי חוזר לאורך זמן ולא רק בשבוע האחרון.
        self.user_days[user.id].add(today)
        self.user_first_day.setdefault(user.id, today)
        self._prune_daily()

    def retention_breakdown(self) -> dict:
        """
        כמה משתמשים חזרו לבוט ביותר מיום אחד. "חוזר" = היה פעיל ב-2 תאריכים
        שונים לפחות (לפי שעון ישראל), לא רק שלח כמה הודעות באותו יום.
        """
        today = self._today()
        buckets = {"1": 0, "2-3": 0, "4-6": 0, "7+": 0}
        total_days = 0
        for days in self.user_days.values():
            n = len(days)
            total_days += n
            if n == 1:
                buckets["1"] += 1
            elif n <= 3:
                buckets["2-3"] += 1
            elif n <= 6:
                buckets["4-6"] += 1
            else:
                buckets["7+"] += 1

        users = len(self.user_days)
        returning = users - buckets["1"]

        # רק משתמשים שהגיעו לפני היום באמת *יכלו* לחזור ביום אחר - מדידה
        # נקייה יותר מאשר לכלול גם את מי שנרשם לפני חצי שעה.
        had_chance = [uid for uid, first in self.user_first_day.items() if first < today]
        came_back = [uid for uid in had_chance if len(self.user_days[uid]) > 1]

        today_users = self.daily_users.get(today, set())
        returning_today = [uid for uid in today_users if self.user_first_day.get(uid, today) < today]

        return {
            "users": users,
            "returning": returning,
            "buckets": buckets,
            "avg_days": (total_days / users) if users else 0,
            "had_chance": len(had_chance),
            "came_back": len(came_back),
            "today_total": len(today_users),
            "today_returning": len(returning_today),
        }

    def record_start(self, user) -> bool:
        """מחזיר True אם זה /start ראשון של המשתמש מאז ההפעלה."""
        self.inc("starts")
        if not user:
            return False
        first = user.id not in self.start_users and user.id not in self.all_users
        self.start_users.add(user.id)
        if first:
            self.inc("new_users")
        return first

    def record_referral(self, referrer_id: int, referrer_name: str, new_user) -> bool:
        """מחזיר True אם נספר כהפניה חדשה (לא הפניה עצמית, לא כפולה)."""
        self.inc("referral_starts")
        if not new_user or new_user.id == referrer_id or new_user.id in self.referred_users:
            return False
        self.referred_users.add(new_user.id)
        self.referral_counts[referrer_id] += 1
        if referrer_name:
            self.referrer_names[referrer_id] = referrer_name
        self.inc("referred_new_users")
        return True

    def record_fetch_time(self, seconds: float) -> None:
        self.fetch_times.append(seconds)

    def record_product(self, item: dict, user) -> None:
        item_id = item.get("item_id")
        if not item_id:
            return
        self.product_counts[item_id] += 1
        if item.get("title"):
            self.product_titles[item_id] = item["title"]
        if user:
            self.product_users[item_id].add(user.id)
        # שומרים על גודל סביר: אם עברנו 3000 מוצרים, זורקים את הנדירים
        if len(self.product_counts) > 3000:
            for pid, _ in self.product_counts.most_common()[2000:]:
                self.product_counts.pop(pid, None)
                self.product_titles.pop(pid, None)
                self.product_users.pop(pid, None)

    def record_alt_search(self, search_stats: dict, matches_count: int) -> None:
        self.inc("alt_searches")
        if matches_count:
            self.inc("alt_found")
            self.inc("alt_matches_total", matches_count)
        for key in ("raw_candidates", "rejected_price", "rejected_accessory",
                    "rejected_title", "rejected_image", "passed"):
            self.alt_rejections[key] += search_stats.get(key, 0)

    # ------------------------------------------------------------------ #
    # Report
    # ------------------------------------------------------------------ #
    def format_report(self, api_stats: dict, active_carts: int = 0, pending_users: int = 0) -> str:
        t = self.totals
        cs = self.currency_symbol
        today = self._today()
        yesterday = (datetime.now(ISRAEL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        started = datetime.fromtimestamp(self.started_at, ISRAEL_TZ).strftime("%d.%m %H:%M")
        uptime = _fmt_duration(time.time() - self.started_at)

        lines = [f"📊 סטטיסטיקות{(' ' + self.label) if self.label else ''} - מאז {started} ({uptime})"]

        # --- משתמשים ---
        lines.append("\n👥 משתמשים")
        lines.append(f"ייחודיים מאז ההפעלה: {len(self.all_users)} | חדשים (/start ראשון): {t['new_users']}")
        lines.append(
            f"היום: {len(self.daily_users.get(today, ()))} | "
            f"אתמול: {len(self.daily_users.get(yesterday, ()))}"
        )
        week = sorted(self.daily_users.items())[-DAILY_HISTORY_DAYS:]
        if len(week) > 1:
            lines.append("לפי יום: " + " · ".join(f"{d[8:10]}.{d[5:7]}: {len(u)}" for d, u in week))
        lines.append(f"עגלות פעילות כרגע: {active_carts} | מוצרים בהמתנה אצל: {pending_users} משתמשים")

        # --- חזרה ושימור ---
        ret = self.retention_breakdown()
        b = ret["buckets"]
        lines.append("\n🔁 חזרה (משתמש שהיה פעיל ב-2 ימים שונים לפחות)")
        lines.append(
            f"חוזרים: {ret['returning']} מתוך {ret['users']} "
            f"({_pct(ret['returning'], ret['users'])}) | ממוצע ימים פעילים: {ret['avg_days']:.1f}"
        )
        lines.append(
            f"פילוח: יום אחד בלבד {b['1']} · 2-3 ימים {b['2-3']} · "
            f"4-6 ימים {b['4-6']} · 7+ ימים {b['7+']}"
        )
        lines.append(
            f"מבין מי שהגיע לפני היום ({ret['had_chance']}) - חזרו ביום אחר: "
            f"{ret['came_back']} ({_pct(ret['came_back'], ret['had_chance'])})"
        )
        lines.append(
            f"היום: {ret['today_total']} פעילים, מתוכם ותיקים שחזרו: "
            f"{ret['today_returning']} ({_pct(ret['today_returning'], ret['today_total'])})"
        )

        # --- קישורים ומוצרים ---
        received = t["links_received"]
        priced = t["items_priced"]
        catalog_less = t["items_catalog_less"]
        lines.append("\n🔗 קישורים")
        lines.append(f"התקבלו: {received} (היום: {self.daily[today]['links_received']})")
        lines.append(f"נשלפו עם מחיר: {priced} ({_pct(priced, received)})")
        lines.append(
            f"בלי פרטים מלאים: {catalog_less} ({_pct(catalog_less, received)}) - "
            f"מתוכם ניתוב אזורי זר: {t['foreign_routing']}"
        )
        lines.append(f"נכשלו לגמרי: {t['items_failed']} | שגיאות חריגות: {t['fetch_exceptions']}")
        lines.append(f"בלי קישור אפיליאייט (בלי עמלה!): {t['affiliate_fallback']}")
        lines.append(f"לא מאומתים למשלוח: {t['not_shipping']}")
        lines.append(f"חסימות קצב (משתמשים ששלחו יותר מדי): {t['rate_limited']}")
        if priced:
            avg_price = t["price_sum"] / priced
            lines.append(f"מחיר ממוצע למוצר שנשלף: {cs}{avg_price:.2f}")

        # --- משפך: עגלה וקופונים ---
        lines.append("\n🛒 משפך")
        singles = t["single_item_cards"]
        lines.append(
            f"כרטיסי מוצר בודד: {singles} → הוספה לעגלה: {t['add_to_cart']} "
            f"({_pct(t['add_to_cart'], singles)})"
        )
        if t["add_to_cart"]:
            lines.append(f"שווי מוצרים שנוספו לעגלות: {cs}{t['cart_value_added']:.2f}")
        lines.append(f"צפיות ב-/cart: {t['cart_views']} | ניקויי עגלה: {t['cart_clears']}")
        lines.append(
            f"קוד הנחה הוצג: {t['coupon_shown']} ({_pct(t['coupon_shown'], priced)} מהמוצרים) | "
            f"הצעת \"עוד X למדרגה הבאה\": {t['upsell_shown']}"
        )

        # --- שיתופים והפניות ---
        lines.append("\n📤 שיתופים")
        lines.append(
            f"הגיעו דרך קישור שיתוף: {t['referral_starts']} "
            f"(משתמשים חדשים ייחודיים: {t['referred_new_users']})"
        )
        top_ref = self.referral_counts.most_common(3)
        if top_ref:
            lines.append(
                "משתפים מובילים: " + ", ".join(
                    f"{self.referrer_names.get(rid, rid)} ({n})" for rid, n in top_ref
                )
            )

        # --- חלופות ---
        lines.append("\n🔍 חיפוש חלופה")
        lines.append(
            f"חיפושים: {t['alt_searches']} | נמצאה חלופה: {t['alt_found']} "
            f"({_pct(t['alt_found'], t['alt_searches'])}) | חסימות קצב: {t['alt_rate_limited']}"
        )
        r = self.alt_rejections
        if r["raw_candidates"]:
            lines.append(
                f"מועמדים: {r['raw_candidates']} → נפסלו: מחיר {r['rejected_price']}, "
                f"אביזר {r['rejected_accessory']}, כותרת {r['rejected_title']}, "
                f"תמונה {r['rejected_image']} | עברו: {r['passed']}"
            )

        # --- ביצועים ו-API ---
        lines.append("\n⚙️ ביצועים ו-API")
        if self.fetch_times:
            times = sorted(self.fetch_times)
            avg = sum(times) / len(times)
            p90 = times[min(len(times) - 1, int(len(times) * 0.9))]
            lines.append(f"זמן שליפת מוצר: ממוצע {avg:.1f}ש' | 90% מתחת ל-{p90:.1f}ש' | מקס' {times[-1]:.1f}ש'")
        calls = api_stats.get("calls", 0)
        lines.append(
            f"קריאות API: {calls} | חסימות ApiCallLimit: {api_stats.get('call_limit_hits', 0)} | "
            f"ניסיונות שמוצו: {api_stats.get('retries_exhausted', 0)} | שגיאות אחרות: {api_stats.get('api_errors', 0)}"
        )
        by_method = api_stats.get("by_method", {})
        if by_method:
            short = {
                "aliexpress.affiliate.productdetail.get": "פרטי מוצר",
                "aliexpress.affiliate.link.generate": "קישורים",
                "aliexpress.affiliate.product.query": "חיפוש",
            }
            lines.append("לפי סוג: " + ", ".join(f"{short.get(m, m)} {n}" for m, n in by_method.items()))
        ph, pm = api_stats.get("product_cache_hits", 0), api_stats.get("product_cache_misses", 0)
        lh, lm = api_stats.get("link_cache_hits", 0), api_stats.get("link_cache_misses", 0)
        lines.append(
            f"Cache מוצרים: {_pct(ph, ph + pm)} פגיעות ({ph}/{ph + pm}) | "
            f"Cache קישורים: {_pct(lh, lh + lm)} ({lh}/{lh + lm})"
        )

        # --- מוצרים פופולריים ---
        top = self.product_counts.most_common(5)
        if top:
            lines.append("\n🔥 המוצרים הכי נשלחים")
            for i, (pid, n) in enumerate(top, start=1):
                title = (self.product_titles.get(pid) or f"#{pid}")[:45]
                users = len(self.product_users.get(pid, ()))
                lines.append(f"{i}. {title} - {n} פעמים ({users} משתמשים)")

        return "\n".join(lines)
