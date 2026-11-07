"""A plain-text list of listings for channels that cannot show property cards.

The website renders every retrieved listing as a card under the answer. WhatsApp is text only, so
the assistant's short answer would point at cards that do not exist. This builds the same facts
as one compact line per listing, straight from the stored listing data (never from the model), in
the order ``candidates`` keeps them, so "the second one" and "compare the first and third" refer
to exactly the numbers shown here.
"""

from __future__ import annotations

from typing import Any

from app.agents.state import _TYPE_AR
from app.helpers.locations import district_label

# An apartment under this many square metres is a data error, not a listing to show.
_MIN_PLAUSIBLE_AREA = 20


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _line(position: int, listing: dict[str, Any], language: str) -> str:
    ar = language == "ar"
    metadata = listing.get("metadata") or {}
    kind = str(metadata.get("property_type") or "")
    kind = _TYPE_AR.get(kind, kind) if ar else kind
    district = district_label(metadata.get("district"), language) or ""
    compound = str(metadata.get("location") or "")
    place = compound if compound and compound != metadata.get("district") else ""
    where = "، ".join(part for part in (place, district) if part) if ar else ", ".join(part for part in (place, district) if part)

    head = f"{kind or ('عقار' if ar else 'Property')}" + (f" {'في' if ar else 'in'} {where}" if where else "")
    facts: list[str] = []
    area = _number(metadata.get("area"))
    if area and area >= _MIN_PLAUSIBLE_AREA:
        facts.append(f"{area:,.0f} " + ("م²" if ar else "m²"))
    bedrooms = _number(metadata.get("bedrooms"))
    if bedrooms:
        facts.append(f"{bedrooms:,.0f} " + ("غرف" if ar else "bedrooms"))
    price = _number(metadata.get("price"))
    if price:
        facts.append(f"{price:,.0f} جنيه" if ar else f"EGP {price:,.0f}")
    if listing.get("property_id"):
        facts.append(("كود " if ar else "ID ") + str(listing["property_id"]))
    return f"{position}) " + " — ".join([head, *facts])


def dedupe_listings(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a unit that the data lists twice (same type, place, size, bedrooms and price)."""
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for listing in listings or []:
        metadata = listing.get("metadata") or {}
        signature = tuple(metadata.get(key) for key in ("property_type", "district", "area", "bedrooms", "price"))
        if all(part is None for part in signature) or signature not in seen:
            seen.add(signature)
            unique.append(listing)
    return unique


def format_property_list(
    listings: list[dict[str, Any]], language: str = "en", limit: int = 5, conditions: str = ""
) -> str:
    """One line per listing plus a short pointer on what to say next; empty when there is nothing.

    ``conditions`` names the search conditions in force, so a condition carried over from an earlier
    message is visible to the customer instead of silently narrowing every search.
    """
    shown = [listing for listing in (listings or []) if listing][:limit]
    if not shown:
        return ""
    ar = language == "ar"
    header = "العقارات المتاحة:" if ar else "Matching listings:"
    lines = [header, *(_line(i, item, language) for i, item in enumerate(shown, 1)), ""]
    if conditions:
        lines.append(
            f'الشروط الحالية: {conditions}. قول "ابدأ من جديد" لو عايز تمسحها.'
            if ar
            else f'Current conditions: {conditions}. Say "start over" to clear them.'
        )
    lines.append(
        'قولّي رقم أو كود أي عقار لتفاصيل أكتر، أو "قارن بينهم".'
        if ar
        else 'Tell me a number or ID for more details, or say "compare them".'
    )
    return "\n".join(lines)
