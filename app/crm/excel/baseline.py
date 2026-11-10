"""What the workbook remembers about each row at export time (pure).

The importer does a three-way merge per field: the value the staff member
typed, the value in the signed baseline (what they were shown) and the value
in PostgreSQL now. These functions define the comparable form of each row so
the exporter and importer can never disagree about it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.crm.excel.text import CELL_LIMIT, clean, fingerprint


def local_date(moment: datetime | None, tz: ZoneInfo) -> str:
    """ISO local date of a UTC moment, or "" - the unit staff edit dates in."""
    if moment is None:
        return ""
    return moment.astimezone(tz).date().isoformat()


def iso_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def lead_baseline(
    *,
    status: str,
    assigned_agent: str | None,
    next_followup_at: datetime | None,
    last_contact: datetime | None,
    notes: str | None,
    tz: ZoneInfo,
) -> dict[str, Any]:
    text = clean(notes)
    return {
        "status": status or "",
        "agent": (assigned_agent or "").strip(),
        "next": local_date(next_followup_at, tz),
        "last": local_date(last_contact, tz),
        "notes": fingerprint(text),
        # A note too long for one cell is shown cut off and must not be edited
        # from the workbook, or the cut-off part would be lost.
        "notes_cut": len(text) > CELL_LIMIT,
    }


def task_baseline(
    *,
    task_type: str,
    description: str | None,
    assigned_agent: str | None,
    due_at: datetime | None,
    priority: str,
    status: str,
    tz: ZoneInfo,
) -> dict[str, Any]:
    return {
        "type": task_type or "",
        "desc": fingerprint(description),
        "agent": (assigned_agent or "").strip(),
        "due": local_date(due_at, tz),
        "priority": priority or "",
        "status": status or "",
    }


def duplicate_baseline(decision: str | None, reviewed_by: str | None) -> dict[str, Any]:
    return {"decision": decision or "", "by": (reviewed_by or "").strip()}


def changed_fields(excel: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    """Fields the staff member edited: the cell differs from what was exported."""
    return [key for key, value in excel.items() if baseline.get(key) != value]
