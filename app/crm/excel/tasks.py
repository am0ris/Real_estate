"""Follow-up tasks derived from records the assistant already keeps.

Nothing in the conversation flow creates these. The sync reads handoffs,
bookings and leads and creates the task each one calls for, so a failure here
can never touch a conversation, and the assistant needs no new hooks.

The sync is idempotent and safe to run from any number of requests at once:
every task carries a ``dedupe_key`` and is inserted with ON CONFLICT DO
NOTHING, so the same situation yields one task however often it is seen. A task
a person cancelled stays cancelled, because its key still exists.

Task keys
    handoff:<id>              a person asked for a sales representative
    booking:<id>:confirm      a viewing request waits for confirmation
    hot:<lead id>             a hot lead nobody has contacted yet
    inactive:<lead id>:<day>  a lead went quiet (one task per quiet period)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crm.excel import schema as sc
from app.crm.excel.queries import OPEN_HANDOFF, chunks, max_by_lead, resolve_booking_leads
from app.crm.excel.schema import bi
from app.crm.excel.text import one_line
from app.crm.repository import OPEN_LEAD_EXCLUDED
from app.database import models as db
from app.database.upsert import insert_ignore
from app.leads.scoring import score_band

logger = logging.getLogger(__name__)

HOT_SCORE = next(score for score in range(101) if score_band(score) == "hot")
# Contacting the customer is what a hot-lead task asks for, so these end it.
CONTACTED = ("contacted", "negotiation")
MAX_NEW_PER_RULE = 500  # tasks created per rule per run: a big backlog is worked off over a few exports
MAX_SCAN = 5_000  # rows read per rule


@dataclass(frozen=True)
class SyncResult:
    created: int = 0
    completed: int = 0
    cancelled: int = 0


def sync_system_tasks(session: Session, *, now: datetime, timezone: str, inactive_days: int = 7) -> SyncResult:
    """Create the tasks that are due and close the ones that no longer apply."""
    tz = ZoneInfo(timezone)
    created = 0
    created += _handoff_tasks(session, now)
    created += _booking_tasks(session, now, tz)
    created += _hot_lead_tasks(session, now)
    created += _inactive_tasks(session, now, inactive_days)
    completed, cancelled = _reconcile(session, now)
    return SyncResult(created, completed, cancelled)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def _create(session: Session, lead: db.Lead, key: str, task_type: str, description: str, priority: str, now: datetime) -> int:
    inserted = insert_ignore(
        session,
        db.CrmTask,
        {
            "public_id": uuid4(),
            "lead_id": lead.id,
            "task_type": task_type,
            "description": description[:500],
            "priority": priority,
            "status": "open",
            "assigned_agent": lead.assigned_agent,
            "due_at": now,
            "source": "system",
            "dedupe_key": key,
            "created_at": now,
            "updated_at": now,
        },
        ["dedupe_key"],
    )
    return 1 if inserted else 0


def _open_leads_filter() -> tuple:
    return (db.Lead.status.not_in(OPEN_LEAD_EXCLUDED),)


def _handoff_tasks(session: Session, now: datetime) -> int:
    rows = session.execute(
        select(db.Handoff, db.Lead)
        .join(db.Lead, db.Lead.id == db.Handoff.lead_id)
        .where(db.Handoff.status.in_(OPEN_HANDOFF), *_open_leads_filter())
        .order_by(db.Handoff.id.desc())
        .limit(MAX_SCAN)
    ).all()
    created = 0
    for handoff, lead in rows:
        if created >= MAX_NEW_PER_RULE:
            break
        reason = one_line(handoff.reason, 80)
        description = bi("Customer asked to speak with a sales representative", "العميل طلب التحدث مع موظف مبيعات")
        if reason:
            description += f" ({reason})"
        priority = "urgent" if handoff.priority == "urgent" else "high"
        created += _create(session, lead, f"handoff:{handoff.id}", "call_customer", description, priority, now)
    return created


def _booking_tasks(session: Session, now: datetime, tz: ZoneInfo) -> int:
    bookings = session.scalars(
        select(db.Booking)
        .where(db.Booking.status == "pending_confirmation", db.Booking.starts_at >= now)
        .order_by(db.Booking.starts_at)
        .limit(MAX_SCAN)
    ).all()
    lead_of = resolve_booking_leads(session, bookings)  # bookings carry no lead_id of their own
    leads: dict[int, db.Lead] = {}
    for chunk in chunks(sorted({lead_id for lead_id in lead_of.values() if lead_id is not None})):
        leads.update({lead.id: lead for lead in session.scalars(select(db.Lead).where(db.Lead.id.in_(chunk)))})
    created = 0
    for booking in bookings:
        if created >= MAX_NEW_PER_RULE:
            break
        lead = leads.get(lead_of.get(booking.id) or 0)
        if lead is None or lead.status in OPEN_LEAD_EXCLUDED:
            continue
        when = booking.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        description = bi(f"Confirm viewing of property {booking.property_id} on {when}", f"تأكيد معاينة العقار {booking.property_id} يوم {when}")
        created += _create(session, lead, f"booking:{booking.id}:confirm", "confirm_viewing", description, "high", now)
    return created


def _active_call_leads(session: Session) -> set[int]:
    return set(
        session.scalars(
            select(db.CrmTask.lead_id).where(db.CrmTask.task_type == "call_customer", db.CrmTask.status.in_(sc.ACTIVE_TASK_STATUSES))
        )
    )


def _hot_lead_tasks(session: Session, now: datetime) -> int:
    already_calling = _active_call_leads(session)
    leads = session.scalars(
        select(db.Lead)
        .where(
            db.Lead.score >= HOT_SCORE,
            db.Lead.status.not_in(OPEN_LEAD_EXCLUDED + CONTACTED),
            db.Lead.last_contacted_at.is_(None),
        )
        .order_by(db.Lead.id.desc())
        .limit(MAX_SCAN)
    ).all()
    description = bi("Hot lead: contact the customer", "عميل ساخن: تواصل مع العميل")
    created = 0
    for lead in leads:
        if created >= MAX_NEW_PER_RULE:
            break
        if lead.id not in already_calling:
            created += _create(session, lead, f"hot:{lead.id}", "call_customer", description, "high", now)
    return created


def _inactive_tasks(session: Session, now: datetime, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = now - timedelta(days=days)
    leads = session.scalars(select(db.Lead).where(*_open_leads_filter()).order_by(db.Lead.id.desc()).limit(MAX_SCAN)).all()
    ids = [lead.id for lead in leads]
    last_message = max_by_lead(session, db.Interaction.lead_id, db.Interaction.created_at, ids)
    handoff_done = max_by_lead(session, db.Handoff.lead_id, db.Handoff.resolved_at, ids, db.Handoff.resolved_at.is_not(None))
    busy = set(
        session.scalars(select(db.CrmTask.lead_id).where(db.CrmTask.status.in_(sc.ACTIVE_TASK_STATUSES)).distinct())
    )
    description = bi(f"No customer message for {days} days: re-engage", f"العميل متوقف من {days} يوم: تواصل معه")
    created = 0
    for lead in leads:
        if created >= MAX_NEW_PER_RULE:
            break
        quiet_since = max(moment for moment in (last_message.get(lead.id), lead.created_at) if moment is not None)
        last_contact = lead.last_contacted_at or handoff_done.get(lead.id)
        if quiet_since > cutoff or (last_contact is not None and last_contact > cutoff) or lead.id in busy:
            continue
        key = f"inactive:{lead.id}:{quiet_since.date().isoformat()}"
        created += _create(session, lead, key, "reengage_inactive", description, "normal", now)
    return created


# ---------------------------------------------------------------------------
# Closing tasks that no longer apply (system tasks only; staff tasks are theirs)
# ---------------------------------------------------------------------------


def _key_id(key: str | None) -> int | None:
    parts = (key or "").split(":")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def _reconcile(session: Session, now: datetime) -> tuple[int, int]:
    tasks = session.scalars(
        select(db.CrmTask)
        .where(db.CrmTask.source == "system", db.CrmTask.status.in_(sc.ACTIVE_TASK_STATUSES))
        .order_by(db.CrmTask.id)  # same row order everywhere: two syncs can never deadlock each other
    ).all()
    if not tasks:
        return 0, 0

    lead_ids = sorted({task.lead_id for task in tasks})
    leads: dict[int, db.Lead] = {}
    for chunk in chunks(lead_ids):
        leads.update({lead.id: lead for lead in session.scalars(select(db.Lead).where(db.Lead.id.in_(chunk)))})
    handoff_ids = sorted({_key_id(t.dedupe_key) for t in tasks if (t.dedupe_key or "").startswith("handoff:")} - {None})
    booking_ids = sorted({_key_id(t.dedupe_key) for t in tasks if (t.dedupe_key or "").startswith("booking:")} - {None})
    handoffs = {h.id: h for h in session.scalars(select(db.Handoff).where(db.Handoff.id.in_(handoff_ids)))} if handoff_ids else {}
    bookings = {b.id: b for b in session.scalars(select(db.Booking).where(db.Booking.id.in_(booking_ids)))} if booking_ids else {}
    last_message = max_by_lead(session, db.Interaction.lead_id, db.Interaction.created_at, lead_ids)

    completed = cancelled = 0
    for task in tasks:
        lead = leads.get(task.lead_id)
        if lead is None:
            continue
        kind = (task.dedupe_key or "").split(":", 1)[0]
        outcome: tuple[str, datetime | None] | None = None

        if lead.status in OPEN_LEAD_EXCLUDED:
            outcome = ("cancelled", None)
        elif kind == "handoff":
            handoff = handoffs.get(_key_id(task.dedupe_key))
            if handoff is not None and handoff.status == "resolved":
                outcome = ("done", handoff.resolved_at)
        elif kind == "booking":
            booking = bookings.get(_key_id(task.dedupe_key))
            if booking is not None and booking.status in ("confirmed", "completed"):
                outcome = ("done", None)
            elif booking is not None and booking.status in ("cancelled", "expired"):
                outcome = ("cancelled", None)
        elif kind == "hot":
            if lead.status in CONTACTED or lead.last_contacted_at is not None:
                outcome = ("done", lead.last_contacted_at)
        elif kind == "inactive":
            returned = last_message.get(lead.id)
            if returned is not None and task.created_at is not None and returned > task.created_at:
                outcome = ("cancelled", None)  # the customer wrote back on their own

        if outcome is None:
            continue
        status, moment = outcome
        task.status = status
        if status == "done":
            task.completed_at = moment or now
            completed += 1
        else:
            cancelled += 1
    return completed, cancelled
