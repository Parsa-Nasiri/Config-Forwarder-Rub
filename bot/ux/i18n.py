"""Bilingual string table (English + Persian).

Add a language by dropping another dict in here and listing its code in
``bot.storage.base.SUPPORTED_LANGUAGES``.
"""

from __future__ import annotations

import re
from string import Formatter

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # --- onboarding -------------------------------------------------
        "welcome_title": "Connected to the config feed",
        "welcome_body": (
            "I watch {channels} Telegram channel(s) around the clock, pull every "
            "proxy config out of them, throw away the duplicates and the junk, "
            "score what is left, and hand you only the good ones.\n\n"
            "You are getting batches of up to {batch} configs. Nothing is sent "
            "below a quality score of {min_score}."
        ),
        "welcome_hint": "Tap a button below or type /help to see everything I can do.",
        "help_body": (
            "Commands\n"
            "/start — subscribe to the feed\n"
            "/latest — get the best configs right now\n"
            "/filters — choose protocols and quality threshold\n"
            "/live — instant delivery for top-tier configs\n"
            "/pause 1h — snooze the feed (or /resume)\n"
            "/stats — your numbers and mine\n"
            "/lang — switch language\n"
            "/help — this message\n\n"
            "How ranking works\n"
            "Every config is scored 0-100 from its protocol, encryption, "
            "freshness, how many independent channels reposted it, the track "
            "record of the channel it came from and what other users reported. "
            "Mark one as dead and everything from that server and that channel "
            "drops in the rankings too."
        ),
        # --- digest ------------------------------------------------------
        "digest_title": "{count} fresh config{s}",
        "digest_empty": "Nothing new worth sending right now. I am still watching.",
        "digest_footer": "Batch #{seq} · {count} config{s} · quality gate {min_score}+",
        "fresh_now": "just now",
        "fresh_min": "{n}m ago",
        "fresh_hour": "{n}h ago",
        "seen_times": "seen {n}×",
        "unknown_geo": "Unknown location",
        "line_quality": "quality {score}/100 · {grade}",
        "grade_excellent": "excellent",
        "grade_good": "good",
        "grade_fair": "fair",
        "grade_weak": "weak",
        # --- interactions -------------------------------------------------
        "copy_header": "Config #{idx} — long-press to copy",
        "copy_all_header": "{count} configs — copy the block below",
        "dead_thanks": "Marked as dead. That server and its source channel both lost points — thanks for the report.",
        "live_thanks": "Noted as working. This protocol will rank higher for you from now on.",
        "nothing_to_send": "There is nothing new in your queue right now.",
        # --- settings ------------------------------------------------------
        "filters_title": "Filters",
        "filters_body": (
            "Protocols: {protocols}\n"
            "Minimum quality score: {min_score}\n"
            "Configs per batch: {batch}\n"
            "Instant delivery: {live}"
        ),
        "filters_all": "all",
        "filters_updated": "Filter updated → {value}",
        "filters_protocols_updated": "Protocols set to {value}",
        "paused_for": "Feed snoozed for {hours}h. Send /resume to wake me up earlier.",
        "paused_forever": "Feed paused. Send /resume to wake me up.",
        "resumed": "Feed resumed — welcome back.",
        "lang_title": "Choose your language",
        "lang_changed": "Language switched to English.",
        "live_on": "Instant delivery is ON — configs scoring {score}+ arrive the moment they are found.",
        "live_off": "Instant delivery is OFF — you get tidy batches instead.",
        # --- stats ---------------------------------------------------------
        "stats_title": "Your stats",
        "stats_body": (
            "Configs delivered to you: {delivered}\n"
            "Received in the last hour: {last_hour}\n"
            "Your quality gate: {min_score}+\n"
            "Protocols: {protocols}\n"
            "Instant delivery: {live}"
        ),
        "stats_global": (
            "\nNetwork\n"
            "Unique configs indexed: {configs}\n"
            "Active users: {users}\n"
            "Total deliveries: {deliveries}"
        ),
        # --- misc -----------------------------------------------------------
        "unknown": "I did not understand that. Type /help to see what I can do.",
        "not_configured": "This command needs a setting the operator has not enabled yet.",
        "error_generic": "Something went wrong on my side. Please try again in a moment.",
        "on": "on",
        "off": "off",
        # --- buttons ---------------------------------------------------------
        "btn_send_now": "Send now",
        "btn_stats": "Stats",
        "btn_filters": "Filters",
        "btn_pause": "Pause",
        "btn_help": "Help",
        "btn_all": "Copy all",
        "btn_lang": "Language",
        "btn_copy": "Copy",
        "btn_dead": "Dead",
        "btn_works": "Works",
        "btn_back": "Back",
        "btn_menu": "Menu",
        "btn_top": "Top configs",
        "btn_score": "Quality",
        "btn_protocols": "Protocols",
        "btn_batchsize": "Batch size",
        "btn_close": "Close",
    },
    "fa": {
        # --- onboarding -------------------------------------------------
        "welcome_title": "به فید کانفیگ‌ها وصل شدید",
        "welcome_body": (
            "من {channels} کانال تلگرامی را شبانه‌روزی زیر نظر دارم، همه‌ی کانفیگ‌ها را "
            "استخراج می‌کنم، تکراری‌ها و بی‌کیفیت‌ها را حذف می‌کنم، به بقیه امتیاز می‌دهم "
            "و فقط کانفیگ‌های خوب را برای شما می‌فرستم.\n\n"
            "در هر نوبت تا {batch} کانفیگ دریافت می‌کنید و چیزی با امتیاز کمتر از "
            "{min_score} برای شما ارسال نمی‌شود."
        ),
        "welcome_hint": "روی دکمه‌های پایین بزنید یا /help را بفرستید.",
        "help_body": (
            "دستورات\n"
            "/start — فعال‌سازی دریافت کانفیگ\n"
            "/latest — بهترین کانفیگ‌ها همین الان\n"
            "/filters — انتخاب پروتکل و حداقل امتیاز\n"
            "/live — دریافت فوری کانفیگ‌های عالی\n"
            "/pause 1h — توقف موقت فید (یا /resume)\n"
            "/stats — آمار شما و من\n"
            "/lang — تغییر زبان\n"
            "/help — همین پیام\n\n"
            "امتیازدهی چطور کار می‌کند\n"
            "هر کانفیگ از ۰ تا ۱۰۰ بر اساس پروتکل، رمزنگاری، تازگی، تعداد کانال‌هایی "
            "که آن را بازنشر کرده‌اند، سابقه‌ی کانال مبدأ و گزارش کاربران امتیاز می‌گیرد. "
            "وقتی یک کانفیگ را «خراب» علامت می‌زنید، آن سرور و کل آن کانال هم امتیاز "
            "از دست می‌دهند."
        ),
        # --- digest ------------------------------------------------------
        "digest_title": "{count} کانفیگ تازه",
        "digest_empty": "الان چیز ارزشمندی برای ارسال نیست. همچنان در حال بررسی‌ام.",
        "digest_footer": "بسته #{seq} · {count} کانفیگ · حد کیفیت {min_score}+",
        "fresh_now": "همین الان",
        "fresh_min": "{n} دقیقه پیش",
        "fresh_hour": "{n} ساعت پیش",
        "seen_times": "{n} بار دیده شده",
        "unknown_geo": "موقعیت نامشخص",
        "line_quality": "کیفیت {score}/100 · {grade}",
        "grade_excellent": "عالی",
        "grade_good": "خوب",
        "grade_fair": "متوسط",
        "grade_weak": "ضعیف",
        # --- interactions -------------------------------------------------
        "copy_header": "کانفیگ #{idx} — برای کپی نگه دارید",
        "copy_all_header": "{count} کانفیگ — بلوک زیر را کپی کنید",
        "dead_thanks": "به عنوان خراب ثبت شد. آن سرور و کانال مبدأ هر دو امتیاز از دست دادند — ممنون از گزارش.",
        "live_thanks": "ثبت شد که کار می‌کند. از این به بعد این پروتکل برای شما بالاتر می‌آید.",
        "nothing_to_send": "الان چیز جدیدی در صف شما نیست.",
        # --- settings ------------------------------------------------------
        "filters_title": "فیلترها",
        "filters_body": (
            "پروتکل‌ها: {protocols}\n"
            "حداقل امتیاز کیفیت: {min_score}\n"
            "تعداد کانفیگ در هر بسته: {batch}\n"
            "ارسال فوری: {live}"
        ),
        "filters_all": "همه",
        "filters_updated": "فیلتر به‌روزرسانی شد → {value}",
        "filters_protocols_updated": "پروتکل‌ها روی {value} تنظیم شد",
        "paused_for": "فید به مدت {hours} ساعت خواباند. با /resume زودتر بیدارش کنید.",
        "paused_forever": "فید متوقف شد. با /resume دوباره فعالش کنید.",
        "resumed": "فید دوباره فعال شد — خوش برگشتید.",
        "lang_title": "زبان خود را انتخاب کنید",
        "lang_changed": "زبان به فارسی تغییر کرد.",
        "live_on": "ارسال فوری روشن است — کانفیگ‌های بالای {score} همان لحظه می‌رسند.",
        "live_off": "ارسال فوری خاموش است — بسته‌های منظم دریافت می‌کنید.",
        # --- stats ---------------------------------------------------------
        "stats_title": "آمار شما",
        "stats_body": (
            "کانفیگ‌های ارسال‌شده به شما: {delivered}\n"
            "دریافتی در یک ساعت گذشته: {last_hour}\n"
            "حد کیفیت شما: {min_score}+\n"
            "پروتکل‌ها: {protocols}\n"
            "ارسال فوری: {live}"
        ),
        "stats_global": (
            "\nشبکه\n"
            "کانفیگ‌های یکتا: {configs}\n"
            "کاربران فعال: {users}\n"
            "تعداد کل ارسال‌ها: {deliveries}"
        ),
        # --- misc -----------------------------------------------------------
        "unknown": "متوجه نشدم. /help را بفرستید تا ببینید چه کارهایی می‌توانم بکنم.",
        "not_configured": "این دستور به تنظیمی نیاز دارد که مدیر هنوز فعال نکرده است.",
        "error_generic": "مشکلی پیش آمد. لحظه‌ای دیگر دوباره تلاش کنید.",
        "on": "روشن",
        "off": "خاموش",
        # --- buttons ---------------------------------------------------------
        "btn_send_now": "ارسال الان",
        "btn_stats": "آمار",
        "btn_filters": "فیلترها",
        "btn_pause": "توقف",
        "btn_help": "راهنما",
        "btn_all": "کپی همه",
        "btn_lang": "زبان",
        "btn_copy": "کپی",
        "btn_dead": "خراب",
        "btn_works": "سالم",
        "btn_back": "بازگشت",
        "btn_menu": "منو",
        "btn_top": "بهترین‌ها",
        "btn_score": "کیفیت",
        "btn_protocols": "پروتکل‌ها",
        "btn_batchsize": "تعداد",
        "btn_close": "بستن",
    },
}

_FIELD_RE = re.compile(r"\{([a-z_]+)\}")


def _safe_format(template: str, **kwargs: object) -> str:
    """``str.format`` that leaves unknown placeholders alone."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(kwargs[key]) if key in kwargs else match.group(0)

    return _FIELD_RE.sub(replace, template)


def t(lang: str, key: str, **kwargs: object) -> str:
    """Translate ``key`` for ``lang``, falling back to English."""
    table = STRINGS.get(lang) or STRINGS["en"]
    template = table.get(key) or STRINGS["en"].get(key) or key
    return _safe_format(template, **kwargs)


def plural_en(count: int) -> str:
    return "" if count == 1 else "s"


__all__ = ["STRINGS", "t", "plural_en"]
