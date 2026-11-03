"""Deterministic Arabic/English understanding shared by every channel.

Extraction here is intentionally rule-based: it runs on every turn (text,
voice and WhatsApp) without an extra LLM call, is fully testable offline and
only records what the customer literally said. Values are emitted in the
canonical vocabulary produced by ``app.rag.normalization`` so they can be
used directly as Qdrant metadata filters (``"Villa"``, ``"Sheikh Zayed"``,
``"sale"``...).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_text(text: str) -> str:
    """Lowercase, unify Arabic letter variants and digits, drop thousands commas."""
    value = str(text or "").translate(_ARABIC_DIGITS)
    value = re.sub(r"(\d),(?=\d{3}(?!\d))", r"\1", value)
    value = re.sub(r"[ً-ٟـ]", "", value)
    value = re.sub(r"[أإآ]", "ا", value).replace("ى", "ي").replace("ة", "ه")
    return value.lower()


def detect_language(text: str, fallback: str = "en") -> str:
    arabic = len(re.findall(r"[؀-ۿ]", text or ""))
    latin = len(re.findall(r"[a-zA-Z]", text or ""))
    if not arabic and not latin:
        return fallback
    return "ar" if arabic >= latin else "en"


# ---------------------------------------------------------------------------
# Vocabulary (canonical values match the ingestion normalizer)
# ---------------------------------------------------------------------------

PROPERTY_TYPES: list[tuple[str, str]] = [
    ("Twin House", r"twin\s*house|توين\s*هاوس"),
    ("Townhouse", r"town\s*house|تاون\s*هاوس"),
    ("Penthouse", r"penthouse|بنتهاوس"),
    ("Duplex", r"duplex|دوبلكس"),
    ("Studio", r"\bstudio|استوديو|ستوديو"),
    ("Villa", r"villa|فيلا|فيلل|فلل|فيلات"),
    ("Apartment", r"apartment|\bflat\b|شقه|شقق"),
    ("Chalet", r"chalet|شاليه"),
    ("Office", r"\boffice|مكتب|اداري"),
    ("Shop", r"\bshop\b|\bretail|محل|تجاري"),
]

DISTRICTS: list[tuple[str, str]] = [
    ("Sheikh Zayed", r"sheikh\s*zayed|\bzayed|الشيخ\s*زايد|زايد"),
    ("New Cairo", r"new\s*cairo|tagamo|tagammu|fifth\s*settlement|التجمع|القاهره\s*الجديده"),
    ("6th of October", r"6th\s*of\s*october|\boctober\b|اكتوبر|6\s*اكتوبر"),
    ("North Coast", r"north\s*coast|sahel|الساحل"),
    ("New Capital", r"new\s*capital|administrative\s*capital|العاصمه\s*الاداريه|العاصمه\s*الجديده"),
    ("Maadi", r"maadi|المعادي"),
    ("Nasr City", r"nasr\s*city|مدينه\s*نصر"),
    ("Mostakbal City", r"mostakbal|المستقبل"),
    ("Ain Sokhna", r"sokhna|السخنه"),
    ("Heliopolis", r"heliopolis|مصر\s*الجديده"),
]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "واحد": 1, "واحده": 1, "اتنين": 2, "اثنين": 2, "تلات": 3, "تلاته": 3, "ثلاث": 3,
    "ثلاثه": 3, "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6,
    "سبع": 7, "سبعه": 7, "تمان": 8, "ثمان": 8, "تسع": 9, "عشر": 10, "عشره": 10,
}
_NUMBER_WORD_RE = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

_BEDROOMS_RE = re.compile(
    rf"(?:(\d{{1,2}})|({_NUMBER_WORD_RE}))\s*(\+)?\s*(?:-\s*)?"
    r"(?:bed(?:room)?s?|br\b|غرف|غرفه|اوض|اوضه)"
    r"(\s*(?:او\s*اكتر|او\s*اكثر|or\s*more|\+))?"
)
_AMOUNT_RE = re.compile(
    rf"(\d+(?:\.\d+)?|{_NUMBER_WORD_RE})\s*"
    r"(million|mil\b|mn\b|m\b|مليون|ملايين|k\b|thousand|الف|ألف)"
)
_BIG_NUMBER_RE = re.compile(r"(?<![\d.])(\d{5,})(?!\d)")
# 10-15 digits, optionally separated by single spaces, dots or dashes ("010 1234 5678",
# "0 1 0 1 2 ..." as speech-to-text often renders them).
_PHONE_RE = re.compile(r"(?<![\d.])(\+?\d(?:[\s.-]?\d){9,14})(?![\d.])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _to_number(token: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    return float(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else None


def _amount(number: str, unit: str) -> float | None:
    value = _to_number(number)
    if value is None:
        return None
    if unit in {"million", "mil", "mn", "m", "مليون", "ملايين"}:
        return value * 1_000_000
    return value * 1_000


def _unique_match(text: str, table: list[tuple[str, str]]) -> str | None:
    hits = [value for value, pattern in table if re.search(pattern, text)]
    # "villa or apartment" is ambiguous: do not guess a filter.
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# Money context
# ---------------------------------------------------------------------------

_DOWN_PAYMENT_HINT = re.compile(r"down\s*payment|deposit|downpayment|مقدم|دفعه\s*اولي|اول\s*دفعه")
_MONTHLY_HINT = re.compile(r"monthly|per\s*month|a\s*month|installment|قسط|شهري|في\s*الشهر|الشهر")
_MIN_HINT = re.compile(r"(above|over|more than|at least|starting|اكتر من|اكثر من|فوق|علي الاقل)\s*$")
_RANGE_RE = re.compile(
    r"(?:between|from|من|بين)\s*(\d+(?:\.\d+)?)\s*(?:(million|m\b|مليون|k\b|الف)\s*)?"
    r"(?:and|to|-|و|ل|الي|لحد)\s*(\d+(?:\.\d+)?)\s*(million|m\b|مليون|ملايين|k\b|الف)"
)


def _money_mentions(text: str) -> list[tuple[float, int, int]]:
    """Return (amount, start, end) for every money-looking value."""
    found: list[tuple[float, int, int]] = []
    for match in _AMOUNT_RE.finditer(text):
        value = _amount(match.group(1), match.group(2))
        if value:
            found.append((value, match.start(), match.end()))
    for match in _BIG_NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for _, start, end in found):
            continue
        # Skip phone numbers (leading 0 / +) — they are contact data, not money.
        if match.group(1).startswith("0") or len(match.group(1)) > 9:
            continue
        found.append((float(match.group(1)), match.start(), match.end()))
    return sorted(found, key=lambda item: item[1])


def _without_phone_numbers(text: str) -> str:
    """Blank out phone numbers so digit groups are never read as prices."""
    return _PHONE_RE.sub(lambda match: " " * len(match.group(0)), text)


def _classified_money(text: str) -> list[tuple[float, str]]:
    """Label each amount as down_payment, monthly, min or price.

    Hint windows stop at the neighbouring amount so "budget 6M, down payment
    500k" does not label the budget as a down payment.
    """
    text = _without_phone_numbers(text)
    mentions = _money_mentions(text)
    labelled: list[tuple[float, str]] = []
    for index, (value, start, end) in enumerate(mentions):
        previous_end = mentions[index - 1][2] if index else 0
        next_start = mentions[index + 1][1] if index + 1 < len(mentions) else len(text)
        before = text[max(previous_end, start - 30):start]
        after = text[end:min(next_start, end + 20)]
        if _DOWN_PAYMENT_HINT.search(before) or re.match(r"\s*(egp|جنيه)?\s*(as\s*)?(down\s*payment|deposit|مقدم)", after):
            labelled.append((value, "down_payment"))
        elif _MONTHLY_HINT.search(before) or _MONTHLY_HINT.search(after):
            labelled.append((value, "monthly"))
        elif _MIN_HINT.search(before):
            labelled.append((value, "min"))
        else:
            labelled.append((value, "price"))
    return labelled


# ---------------------------------------------------------------------------
# Public extraction API
# ---------------------------------------------------------------------------


def extract_requirements(text: str) -> dict[str, Any]:
    """Extract search requirements literally stated in one message."""
    t = normalize_text(text)
    out: dict[str, Any] = {}

    property_type = _unique_match(t, PROPERTY_TYPES)
    if property_type:
        out["property_type"] = property_type
    district = _unique_match(t, DISTRICTS)
    if district:
        out["district"] = district

    if re.search(r"غرفتين|اوضتين", t):
        out["bedrooms"] = 2
    else:
        match = _BEDROOMS_RE.search(_without_phone_numbers(t))
        if match:
            number = _to_number(match.group(1) or match.group(2))
            if number and 0 < number < 20:
                if match.group(3) or match.group(4):
                    out["min_bedrooms"] = int(number)
                else:
                    out["bedrooms"] = int(number)

    rent = bool(re.search(r"\brent|rental|\blease|ايجار|اجار|تاجير", t))
    buy = bool(re.search(r"\bbuy|purchase|for sale|\bsale\b|\bown\b|شراء|اشتري|للبيع|تمليك", t))
    if rent != buy:
        out["transaction_type"] = "rent" if rent else "sale"

    if re.search(r"ready\s*to\s*move|immediate\s*delivery|استلام\s*فوري|جاهز", t):
        out["delivery"] = "ready"
    elif re.search(r"off[\s-]*plan|under\s*construction|تحت\s*الانشاء", t):
        out["delivery"] = "off-plan"

    range_match = _RANGE_RE.search(_without_phone_numbers(t))
    if range_match:
        low_unit = range_match.group(2) or range_match.group(4)
        low = _amount(range_match.group(1), low_unit)
        high = _amount(range_match.group(3), range_match.group(4))
        if low and high and low < high:
            out["min_price"], out["max_price"] = low, high
            return out

    for value, kind in _classified_money(t):
        # Financing amounts are qualification data, not price filters.
        if kind == "min":
            out["min_price"] = value
            break
        if kind == "price":
            out["max_price"] = value
            break
    return out


_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "صفر": "0", "واحد": "1", "اتنين": "2", "اثنين": "2", "تلاته": "3", "ثلاثه": "3", "تلاتة": "3", "ثلاثة": "3",
    "اربعه": "4", "أربعة": "4", "اربعة": "4", "خمسه": "5", "خمسة": "5", "سته": "6", "ستة": "6",
    "سبعه": "7", "سبعة": "7", "تمانيه": "8", "ثمانيه": "8", "تمانية": "8", "ثمانية": "8", "تسعه": "9", "تسعة": "9",
}
_DIGIT_WORD_RUN = re.compile(
    r"(?<![\w])((?:(?:" + "|".join(sorted(map(re.escape, _DIGIT_WORDS), key=len, reverse=True)) + r"|\d)[\s,.-]*){4,})",
    re.IGNORECASE,
)


def _spoken_digits_to_numerals(text: str) -> str:
    """"zero one zero, one two three four" -> "0101234" (phone numbers read aloud)."""
    def convert(match: re.Match) -> str:
        tokens = re.findall(r"\d|[^\W\d]+", match.group(0))
        digits = [_DIGIT_WORDS.get(token.lower(), token) for token in tokens]
        return "".join(digits) + " " if all(d.isdigit() for d in digits) else match.group(0)
    return _DIGIT_WORD_RUN.sub(convert, text)


def extract_qualification(text: str) -> dict[str, Any]:
    """Extract lead-qualification signals literally stated in one message."""
    t = normalize_text(text)
    out: dict[str, Any] = {}

    for value, kind in _classified_money(t):
        if kind == "down_payment":
            out.setdefault("down_payment", value)
        elif kind == "monthly":
            out.setdefault("monthly_installment", value)
        else:
            out.setdefault("budget", value)

    if re.search(r"invest|investment|roi|return|استثمار|استثماري", t):
        out["purpose"] = "investment"
    elif re.search(r"live in|to live|for my family|residen|family home|سكن|اسكن|للعيله|للسكن", t):
        out["purpose"] = "residence"
    elif re.search(r"\brent\b|ايجار", t) and "purpose" not in out:
        out["purpose"] = "rent"

    if re.search(r"\bcash\b|كاش", t):
        out["payment_method"] = "cash"
    elif re.search(r"installment|instalment|payment plan|تقسيط|اقساط|قسط", t):
        out["payment_method"] = "installments"

    if re.search(r"ready\s*to\s*move|immediate|استلام\s*فوري", t):
        out["delivery_preference"] = "ready"
    elif re.search(r"off[\s-]*plan|under\s*construction|تحت\s*الانشاء", t):
        out["delivery_preference"] = "off-plan"

    timeline = re.search(
        r"(asap|immediately|right away|this (?:month|year)|next (?:month|year)|within \d+ (?:months?|years?)|"
        r"in \d+ (?:months?|years?)|حالا|فورا|الشهر ده|السنه دي|الشهر الجاي|السنه الجايه|خلال \d+ (?:شهور|اشهر|سنين|سنه))",
        t,
    )
    if timeline:
        out["timeline"] = timeline.group(1)

    phone = _PHONE_RE.search(_spoken_digits_to_numerals(str(text or "").translate(_ARABIC_DIGITS)))
    if phone:
        digits = re.sub(r"[^\d+]", "", phone.group(1))
        if 10 <= len(digits.lstrip("+")) <= 15:
            out["phone"] = digits
    email = _EMAIL_RE.search(text or "")
    if email:
        out["email"] = email.group(0).lower()
    name = re.search(r"(?:my name is|i am|i'm|اسمي|انا اسمي)\s+([A-Za-z؀-ۿ]{2,20}(?:\s+[A-Za-z؀-ۿ]{2,20})?)", text or "", re.IGNORECASE)
    if name and not re.search(r"\d", name.group(1)):
        candidate = name.group(1).strip()
        if candidate.lower() not in {"looking", "interested", "searching", "here", "ready"}:
            out["name"] = candidate
    return out


# ---------------------------------------------------------------------------
# Property selection ("the second one", "property 501189976")
# ---------------------------------------------------------------------------

# Arabic ordinals accept "ال" and the merged "لل" ("للتاني" = "for the second").
_ORDINALS = {
    1: r"\b(first|1st)\b|(ال|لل)اول|اول واحد|رقم\s*1\b|#1\b",
    2: r"\b(second|2nd)\b|(ال|لل)(تاني|ثاني)|رقم\s*2\b|#2\b",
    3: r"\b(third|3rd)\b|(ال|لل)(تالت|ثالث)|رقم\s*3\b|#3\b",
    4: r"\b(fourth|4th)\b|(ال|لل)رابع|رقم\s*4\b|#4\b",
    5: r"\b(fifth|5th)\b|(ال|لل)خامس|رقم\s*5\b|#5\b",
}


def resolve_property_reference(text: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick a candidate the customer referred to by id or ordinal position."""
    if not candidates:
        return None
    t = normalize_text(text)
    for candidate in candidates:
        property_id = str(candidate.get("property_id") or "")
        if property_id and len(property_id) >= 3 and re.search(rf"(?<![\w-]){re.escape(property_id.lower())}(?![\w-])", t):
            return candidate
    if re.search(r"\b(last one|the last)\b|الاخير", t):
        return candidates[-1]
    for position, pattern in _ORDINALS.items():
        if re.search(pattern, t) and position <= len(candidates):
            return candidates[position - 1]
    return None


# ---------------------------------------------------------------------------
# Dates and times for viewings
# ---------------------------------------------------------------------------

_WEEKDAYS = [
    (0, r"monday|الاتنين|الاثنين"),
    (1, r"tuesday|التلات|الثلاثاء"),
    (2, r"wednesday|الاربع|الاربعاء"),
    (3, r"thursday|الخميس"),
    (4, r"friday|الجمعه"),
    (5, r"saturday|السبت"),
    (6, r"sunday|الحد|الاحد"),
]


def extract_date(text: str, today: date) -> date | None:
    t = normalize_text(text)
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    dmy = re.search(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d:])", t)
    if dmy:
        day, month = int(dmy.group(1)), int(dmy.group(2))
        year = int(dmy.group(3)) if dmy.group(3) else today.year
        year = year + 2000 if year < 100 else year
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if not dmy.group(3) and parsed < today:
            parsed = parsed.replace(year=parsed.year + 1)
        return parsed
    if re.search(r"day after tomorrow|بعد بكره", t):
        return today + timedelta(days=2)
    if re.search(r"tomorrow|بكره|غدا", t):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b|النهارده|اليوم", t):
        return today
    for weekday, pattern in _WEEKDAYS:
        if re.search(pattern, t):
            delta = (weekday - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return None


def extract_time(text: str) -> tuple[int, int] | None:
    """Return (hour, minute) in 24h form when the customer names a time."""
    t = normalize_text(text)
    match = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm|ص|م|صباحا|مساء|الصبح|بالليل|العصر|الضهر)?(?!\d)", t)
    if not match or (match.group(2) is None and match.group(3) is None and not re.search(r"(at|الساعه)\s*\d", t)):
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = match.group(3) or ""
    if suffix in {"pm", "م", "مساء", "بالليل", "العصر"} and hour < 12:
        hour += 12
    elif suffix in {"am", "ص", "صباحا", "الصبح"} and hour == 12:
        hour = 0
    elif not suffix and 1 <= hour <= 7:
        hour += 12  # "at 4" during business hours means 4 pm
    if hour > 23 or minute > 59:
        return None
    return hour, minute


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

_YES = re.compile(r"^\s*(yes|yeah|yep|sure|ok|okay|confirm|confirmed|go ahead|book it|please do|اه|ايوه|ايوا|نعم|تمام|ماشي|اكد|احجز|موافق)\b")
_NO = re.compile(r"^\s*(no|nope|cancel|not now|don't|لا|لأ|مش عايز|الغي|الغاء)\b")


def is_affirmative(text: str) -> bool:
    return bool(_YES.search(normalize_text(text)))


def is_negative(text: str) -> bool:
    return bool(_NO.search(normalize_text(text)))


# An unambiguous "stop this" (a bare "no" is not: mid-flow it often means "not that time").
_ABORT = re.compile(r"^\s*(cancel|never\s*mind|forget it|forget about it|stop|abort|quit|exit|الغي|الغاء|خلاص|بلاش|مش عايز)\b")


def is_abort(text: str) -> bool:
    return bool(_ABORT.search(normalize_text(text)))


# "Start over" wording. A WhatsApp customer keeps one conversation for days or weeks, so filters
# from an old search must be removable in plain words (the website has a reset button and
# removable filter chips for the same purpose). Matching runs on ``normalize_text`` output, so
# ``ابدأ من جديد`` and ``ابدا من جديد`` are the same; a bare "من جديد" ("again") is not enough.
_NEW_SEARCH = re.compile(
    r"\b(?:ابدا|نبدا|ابدء)\s+(?:من\s+(?:جديد|الاول|الصفر)|بحث\s+جديد|تاني)"
    r"|\bبحث\s+جديد"
    r"|\bابحث\s+من\s+جديد"
    r"|\bامسح\s+(?:البحث|الفلاتر|كل\s*شي|كل\s*حاجه)"
    r"|\bstart\s*(?:over|again|fresh)\b"
    r"|\bnew\s+search\b"
    r"|\b(?:reset|clear)\s+(?:my\s+|the\s+)?(?:search|filters|requirements)\b"
    r"|^\s*(?:reset|restart|clear)\s*$"
)


def wants_new_search(text: str) -> bool:
    """True when the customer asks to drop the carried search and begin again."""
    return bool(_NEW_SEARCH.search(normalize_text(text)))


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

INTENT_PATTERNS: list[tuple[str, str]] = [
    ("human_handoff", r"human|real person|agent|representative|sales ?(rep|person|team)|call me|talk to (someone|a person)|"
                      r"speak (to|with) (someone|a person|an agent)|مندوب|موظف|حد يكلمني|كلمني|اكلم حد|بني ادم|خدمه العملاء"),
    ("cancel_visit", r"cancel (my |the |this |that )?(visit|viewing|appointment|booking|tour)|(visit|viewing|appointment|booking|tour) cancel|"
                     r"reschedule|re-schedule|can't make it|cannot make it|"
                     r"(الغ[يى]|الغاء|اكنسل|كنسل)\s*(ال)?(معاينه|ميعاد|معاد|موعد|حجز|زياره)|مش هقدر (اجي|احضر)|غير(ي)? (ال)?(معاد|ميعاد)"),
    ("booking_status", r"(is|was|has) (my|the) (visit|viewing|appointment|booking|tour)( been)? (confirmed|approved)|"
                       r"(status|state) of my (visit|viewing|appointment|booking)|my (booking|viewing|appointment) status|"
                       r"(المعاينه|الميعاد|الحجز|الموعد) (اتاكد|اتأكد|اتاكدت|اتأكدت)|حاله (الحجز|المعاينه)"),
    ("schedule_visit", r"schedule|book (a )?(visit|viewing|tour)|visit|viewing|appointment|see it|tour|معاينه|موعد|ميعاد|زياره|اشوفها|احجز معاد"),
    ("payment_plan", r"payment plan|installment|instalment|down ?payment|monthly|تقسيط|اقساط|قسط|مقدم|نظام السداد"),
    ("investment", r"invest|investment|roi|rental yield|return|resale|استثمار|عائد"),
    ("availability", r"available|availability|still for sale|sold|متاح|متاحه|لسه موجود|اتباع"),
    ("project_information", r"compound|project|developer|amenities|facilities|كمبوند|مشروع|المطور|الخدمات"),
    ("sales", r"price|cost|how much|discount|offer|reserve|negotiate|سعر|بكام|كام|خصم|عرض|احجزها|اشتريها"),
    ("lead_qualification", r"my budget|budget is|i can pay|i want to buy|looking to buy|ميزانيتي|الميزانيه|معايا|اقدر ادفع"),
    ("property_search", r"property|properties|apartment|villa|house|home|unit|chalet|studio|duplex|penthouse|listing|"
                        r"show me|looking for|find|search|عقار|عقارات|شقه|شقق|فيلا|فلل|وحده|شاليه|بيت|دور علي|عايز|محتاج"),
    ("greeting", r"^(hi|hello|hey|good (morning|evening|afternoon)|مرحبا|اهلا|السلام عليكم|سلام|صباح الخير|مساء الخير|ازيك|هاي)\b"),
]


def classify_intent(text: str) -> str:
    t = normalize_text(text)
    for intent, pattern in INTENT_PATTERNS:
        if re.search(pattern, t):
            return intent
    return "conversational"
