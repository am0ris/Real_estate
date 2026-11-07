"""Place names: one table shared by ingestion and the assistant.

The catalogue stores each listing's ``district`` as one spelling ("Ain Sukhna",
"New Capital City"), and the assistant filters on it with an exact match. Customers write
the same places many ways ("التجمع", "نيو كايرو", "5th settlement", "Ain Sokhna"), so every
place has ONE canonical spelling here, and both sides use it:

* ingestion calls :func:`canonical_district` so the stored value is always the canonical one;
* the assistant calls :func:`match_district` to turn what the customer wrote into that value.

A name the assistant produces that no listing carries returns zero results, so
``tests/unit/test_locations.py`` checks the table against the real data files.
"""

from __future__ import annotations

import re

# Every district present in the data files (data/raw/properties), most listings first.
KNOWN_DISTRICTS: tuple[str, ...] = ('New Cairo', 'Sheikh Zayed', '6th of October', 'Mostakbal City', 'Ain Sukhna', 'North Coast', 'New Capital City', 'Madinaty', 'Shorouk City', 'Maadi', 'Hadayek October', 'Nasr City', 'Heliopolis', 'New Heliopolis', 'Hurghada', 'Mokattam', 'Gouna', 'Smoha', 'Sheraton', 'Mohandessin', 'Katameya', 'Alamein', 'Zahraa Al Maadi', 'Ras Sedr', 'Badr City', 'Soma Bay', 'El Fostat', 'Sahl Hasheesh', 'Dokki', 'Obour City', 'Zamalek', 'Makadi Bay', 'Agami', 'Amreya', 'Moharam Bik', 'Laurent', 'Downtown Cairo', 'Miami', 'Sidi Beshr', 'Kafr Abdo', 'Masr al-Kadema', 'San Stefano', '10th of Ramadan', 'King Mariout', 'Ras Soma', 'Asafra', 'Mandara', 'Glim', 'Roushdy', 'Al Manial', 'Hadayek al-Ahram', 'Camp Caesar', 'Zezenia', 'Garden City', 'Almazah', 'Cleopatra', 'Al Ibrahimiyyah', 'Saba Pasha', 'Gianaclis', 'Fleming', 'Sidi Gaber', 'Victoria', 'Borg al-Arab', 'New Mansoura', 'Sporting', 'Bolkly', 'Maamoura', 'Stanley', 'New Nozha', 'Qasr al-Nil', 'Agouza', 'New Sohag', 'Seyouf', 'Tanta', 'Raml Station', 'Sharm al-Sheikh', 'Dahab', 'Marsa Alam', 'Montazah', 'Markaz al-Giza', 'Azarita', 'Haram', 'Ramses + Ramses Extension', 'Mansuriyya', 'Dabaa', 'Shubra', 'Bahray - Anfoshy', 'Abu Qir', 'Badrasheen', 'Zohour District', 'Giza District', 'Gesr Al Suez', 'Helmeyat El Zaytoun', 'Marsa Matrouh', 'Shatby', 'New Assiut', 'Dar al-Salaam', 'Kafr al-Bateekh', 'New Damietta', 'Maryotaya', 'Faisal', 'Abu Talat', 'Ras al-Bar', 'Mansura', 'Ismailia City', 'Fayed')

# Other spellings seen in sources or typed by customers -> canonical spelling.
RAW_SPELLINGS: dict[str, str] = {
    "Ain Sokhna": "Ain Sukhna",
    "AinSukhna": "Ain Sukhna",
    "New Capital": "New Capital City",
    "Administrative Capital": "New Capital City",
    "Sheikh Zayed City": "Sheikh Zayed",
    "El Sheikh Zayed": "Sheikh Zayed",
    "Fifth Settlement": "New Cairo",
    "5th Settlement": "New Cairo",
    "Tagamoa": "New Cairo",
    "October": "6th of October",
    "6th October": "6th of October",
    "Sixth of October": "6th of October",
    "Mostakbal": "Mostakbal City",
    "Shorouk": "Shorouk City",
    "El Shorouk": "Shorouk City",
    "Obour": "Obour City",
    "El Gouna": "Gouna",
    "Smouha": "Smoha",
    "Fustat": "El Fostat",
}

# canonical -> (Arabic label shown to customers, Arabic aliases, extra English patterns).
# Arabic aliases are written the way ``normalize_text`` leaves Arabic (no hamza on alef, ه for
# ة, ي for ى) WITHOUT the leading "ال"; an optional "ال"/"بال"/"لل"... prefix is allowed.
_DETAILS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "New Cairo": ("التجمع (القاهرة الجديدة)", ("تجمع", "القاهره الجديده", "نيو كايرو", "نيوكايرو"), ("tagamo\\w*", "tagammu", "fifth\\s*settlement", "5th\\s*settlement")),
    "Sheikh Zayed": ("الشيخ زايد", ("شيخ زايد", "زايد"), ("\\bzayed",)),
    "6th of October": ("6 أكتوبر", ("اكتوبر", "6 اكتوبر", "السادس من اكتوبر"), ("6th\\s*(?:of\\s*)?october", "6\\s*october", "october\\s*city", "\\boctober\\b")),
    "Hadayek October": ("حدائق أكتوبر", ("حدائق اكتوبر", "حدايق اكتوبر"), ()),
    "Mostakbal City": ("مستقبل سيتي", ("مستقبل سيتي", "مدينه المستقبل", "مدينه مستقبل"), ("mostakb[ae]l", "future\\s*city")),
    "Ain Sukhna": ("العين السخنة", ("سخنه",), ("sokhna", "sukhna")),
    "North Coast": ("الساحل الشمالي", ("ساحل شمالي", "ساحل"), ("north\\s*coast", "\\bsahel")),
    "New Capital City": ("العاصمة الإدارية", ("عاصمه اداريه", "عاصمه جديده", "نيو كابيتال", "نيوكابيتال"), ("new\\s*capital", "administrative\\s*capital")),
    "Madinaty": ("مدينتي", ("مدينتي",), ("madinat[iy]", "madinety")),
    "Shorouk City": ("الشروق", ("مدينه الشروق", "شروق"), ("shorou[kq]",)),
    "Maadi": ("المعادي", ("معادي",), ("\\bmaadi",)),
    "Zahraa Al Maadi": ("زهراء المعادي", ("زهراء المعادي", "زهرا المعادي", "زهره المعادي"), ("zahraa\\s*(?:al|el)?\\s*maadi",)),
    "Nasr City": ("مدينة نصر", ("مدينه نصر",), ("nasr\\s*city",)),
    "Heliopolis": ("مصر الجديدة", ("مصر الجديده", "هليوبوليس"), ("heliopolis",)),
    "New Heliopolis": ("هليوبوليس الجديدة", ("هليوبوليس الجديده", "نيو هليوبوليس"), ("new\\s*heliopolis",)),
    "Hurghada": ("الغردقة", ("غردقه",), ("hurghada",)),
    "Mokattam": ("المقطم", ("مقطم",), ("mokatt?am",)),
    "Gouna": ("الجونة", ("جونه",), ("gouna",)),
    "Smoha": ("سموحة", ("سموحه",), ("smou?ha",)),
    "Sheraton": ("الشيراتون", ("شيراتون",), ("sheraton",)),
    "Mohandessin": ("المهندسين", ("مهندسين",), ("mohand[ei]s+[ei]n",)),
    "Katameya": ("القطامية", ("قطاميه",), ("katam[ey]a", "katamia")),
    "Alamein": ("العلمين", ("علمين",), ("alamein",)),
    "Ras Sedr": ("رأس سدر", ("راس سدر",), ("ras\\s*sedr", "ras\\s*sidr")),
    "Badr City": ("مدينة بدر", ("مدينه بدر",), ("badr\\s*city",)),
    "Soma Bay": ("سوما باي", ("سوما باي",), ("soma\\s*bay",)),
    "El Fostat": ("الفسطاط", ("فسطاط",), ("fostat", "fustat")),
    "Sahl Hasheesh": ("سهل حشيش", ("سهل حشيش",), ("sahl\\s*hash[ei]+sh",)),
    "Dokki": ("الدقي", ("دقي",), ("dokki",)),
    "Obour City": ("العبور", ("مدينه العبور", "عبور"), ("obour",)),
    "Zamalek": ("الزمالك", ("زمالك",), ("zamalek",)),
    "Makadi Bay": ("مكادي باي", ("مكادي",), ("makadi",)),
    "Agami": ("العجمي", ("عجمي",), ("agami",)),
    "Downtown Cairo": ("وسط البلد", ("وسط البلد",), ("downtown",)),
    "Miami": ("ميامي", ("ميامي",), ()),
    "Stanley": ("ستانلي", ("ستانلي",), ()),
    "San Stefano": ("سان ستيفانو", ("سان ستيفانو",), ("san\\s*stefano",)),
    "Sidi Beshr": ("سيدي بشر", ("سيدي بشر",), ("sidi\\s*beshr",)),
    "Laurent": ("لوران", ("لوران",), ()),
    "Roushdy": ("رشدي", ("رشدي",), ("roushdy",)),
    "Cleopatra": ("كليوباترا", ("كليوباترا",), ()),
    "Sidi Gaber": ("سيدي جابر", ("سيدي جابر",), ("sidi\\s*gaber",)),
    "Montazah": ("المنتزه", ("منتزه",), ("montazah",)),
    "Al Manial": ("المنيل", ("منيل",), ("manial",)),
    "Garden City": ("جاردن سيتي", ("جاردن سيتي",), ("garden\\s*city",)),
    "Agouza": ("العجوزة", ("عجوزه",), ("agouza",)),
    "Haram": ("الهرم", ("هرم",), ()),
    "Shubra": ("شبرا", ("شبرا",), ()),
    "10th of Ramadan": ("العاشر من رمضان", ("العاشر من رمضان", "10 رمضان"), ("10th\\s*(?:of\\s*)?ramadan",)),
    "Marsa Alam": ("مرسى علم", ("مرسي علم",), ("marsa\\s*alam",)),
    "Sharm al-Sheikh": ("شرم الشيخ", ("شرم الشيخ",), ("sharm",)),
    "Dahab": ("دهب", ("دهب",), ("dahab",)),
    "Marsa Matrouh": ("مرسى مطروح", ("مرسي مطروح",), ("matrouh", "matruh")),
    "Hadayek al-Ahram": ("حدائق الأهرام", ("حدائق الاهرام",), ("hadayek\\s*(?:al|el)?[-\\s]*ahram",)),
    "New Nozha": ("النزهة الجديدة", ("نزهه جديده",), ("new\\s*nozha",)),
    "Qasr al-Nil": ("قصر النيل", ("قصر النيل",), ("qasr\\s*(?:al|el)?[-\\s]*nil",)),
    "Amreya": ("العامرية", ("عامريه",), ("amreya",)),
    "Borg al-Arab": ("برج العرب", ("برج العرب",), ("borg\\s*(?:al|el)?[-\\s]*arab",)),
}

# English names that are ordinary words or personal names: never matched from the bare name.
_NO_AUTO_ENGLISH = {"Victoria", "Sporting", "Fayed", "Glim", "Mandara", "Bolkly", "Haram", "Laurent", "Miami", "Fleming", "Stanley", "Faisal", "Roushdy", "Cleopatra", "Shubra"}

_AR = r"\u0621-\u064a"
_PREFIX = r"(?:[بلفوك]?ال|لل|[بلفوك])?"


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


_CANONICAL_BY_KEY: dict[str, str] = {_key(name): name for name in KNOWN_DISTRICTS}
_CANONICAL_BY_KEY.update({_key(raw): canonical for raw, canonical in RAW_SPELLINGS.items()})


def canonical_district(raw: str | None) -> str | None:
    """The canonical spelling of a district; an unknown name is returned trimmed, never dropped."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    return _CANONICAL_BY_KEY.get(_key(text), text)


def district_label(canonical: str | None, language: str = "en") -> str | None:
    """How a district is named to a customer ("التجمع (القاهرة الجديدة)" in Arabic)."""
    if not canonical:
        return canonical
    if language == "ar" and canonical in _DETAILS:
        return _DETAILS[canonical][0]
    return canonical


def _english_pattern(name: str) -> str:
    return r"\b" + r"[\s-]*".join(re.escape(token) for token in re.split(r"[\s-]+", name.lower()) if token) + r"\b"


def _arabic_pattern(alias: str) -> str:
    tokens = [re.escape(token) for token in alias.split() if token]
    return rf"(?<![{_AR}]){_PREFIX}" + r"\s*(?:ال)?".join(tokens) + rf"(?![{_AR}])"


def _build_patterns() -> list[tuple[str, re.Pattern[str]]]:
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for name in KNOWN_DISTRICTS:
        details = _DETAILS.get(name)
        parts: list[str] = []
        if name not in _NO_AUTO_ENGLISH and len(name) >= 5 and re.fullmatch(r"[A-Za-z0-9 '\-]+", name):
            parts.append(_english_pattern(name))
        if details:
            parts.extend(details[2])
            parts.extend(_arabic_pattern(alias) for alias in details[1])
        for raw, canonical in RAW_SPELLINGS.items():
            if canonical == name:
                parts.append(_english_pattern(raw))
        if parts:
            patterns.append((name, re.compile("|".join(f"(?:{part})" for part in parts))))
    return patterns


_PATTERNS = _build_patterns()

# (canonical, regex) pairs for callers that want the raw table.
DISTRICTS: list[tuple[str, str]] = [(name, pattern.pattern) for name, pattern in _PATTERNS]


def match_district(normalized_text: str) -> str | None:
    """The one district a message names, or None when it names none or several.

    ``normalized_text`` is the output of ``normalize_text``. When one name sits inside a longer
    one ("حدائق أكتوبر" contains "أكتوبر", "New Heliopolis" contains "Heliopolis") the longer wins.
    """
    spans: list[tuple[int, int, str]] = []
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(normalized_text):
            if match.end() > match.start():
                spans.append((match.start(), match.end(), name))
    kept = {
        name
        for start, end, name in spans
        if not any(o_start <= start and end <= o_end and (o_end - o_start) > (end - start) and o_name != name for o_start, o_end, o_name in spans)
    }
    return next(iter(kept)) if len(kept) == 1 else None
