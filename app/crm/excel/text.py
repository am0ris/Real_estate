"""Text helpers shared by the exporter and importer (pure)."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

# Excel's hard limit is 32,767 characters per cell.
CELL_LIMIT = 32_000
TRUNCATION_MARK = "…[truncated]"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t\u00a0]+")
# A cell starting with one of these is read as a formula by spreadsheet apps.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def clean(value: object, limit: int | None = None) -> str:
    """Printable text: control characters removed, CR/LF normalised to LF."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    if limit is not None and len(text) > limit:
        text = text[: limit - len(TRUNCATION_MARK)] + TRUNCATION_MARK
    return text


def is_formula_like(text: str) -> bool:
    return text.startswith(FORMULA_TRIGGERS)


def excerpt(value: object, limit: int = 300) -> str:
    """One-line excerpt of a message for the interaction log.

    The conversation itself stays in PostgreSQL; the workbook only needs enough
    to recognise the exchange. Cut at a word boundary when one is near.
    """
    text = _SPACES.sub(" ", clean(value).replace("\n", " ")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = cut.rfind(" ")
    if boundary >= limit - 40:
        cut = cut[:boundary]
    return cut.rstrip(" ,.;:-") + "…"


def one_line(value: object, limit: int) -> str:
    return excerpt(value, limit)


# ---------------------------------------------------------------------------
# Human-readable record codes (the workbook never shows raw database ids)
# ---------------------------------------------------------------------------

LEAD_PREFIX, TASK_PREFIX, INTERACTION_PREFIX, BOOKING_PREFIX, PERSON_PREFIX = "L", "T", "I", "B", "P"


def code(prefix: str, pk: int) -> str:
    return f"{prefix}-{int(pk):06d}"


def parse_code(prefix: str, value: object) -> int | None:
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d{{1,12}})", str(value or "").strip().upper())
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Baseline fingerprints
# ---------------------------------------------------------------------------


def fingerprint(value: object) -> str:
    """Compact, comparable form of a field value for the signed baseline.

    Short values are kept readable; long ones (notes) are hashed so the hidden
    sheet stays small. Whitespace at the ends is not significant.
    """
    text = clean(value).strip()
    if len(text) <= 64:
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Requirement summary
# ---------------------------------------------------------------------------

# Already shown in their own columns.
_DEDICATED = {"transaction_type", "district", "location", "property_type", "min_price", "max_price", "budget", "min_bedrooms", "bedrooms"}
_PREFERRED_ORDER = ("compound", "min_area", "max_area", "bathrooms", "finishing", "delivery", "furnished", "amenities", "payment_plan")


def requirements_summary(requirements: Mapping[str, Any] | None, limit: int = 220) -> str:
    """``key: value; key: value`` for the requirements not shown elsewhere."""
    if not isinstance(requirements, Mapping):
        return ""
    keys = [key for key in _PREFERRED_ORDER if key in requirements]
    keys += sorted(key for key in requirements if key not in _PREFERRED_ORDER)
    parts: list[str] = []
    for key in keys:
        if key in _DEDICATED:
            continue
        value = requirements.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item) for item in value)
        elif isinstance(value, Mapping):
            value = ", ".join(f"{k}={v}" for k, v in value.items())
        parts.append(f"{key}: {value}")
    return excerpt("; ".join(parts), limit)
