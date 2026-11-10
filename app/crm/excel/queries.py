"""Read-only projection of PostgreSQL into :class:`ExportData`.

This is the only part of the export that touches the database, and it only
reads. It never calls the assistant, the RAG pipeline or any external
service, so exporting cannot slow down or disturb a conversation.

On PostgreSQL the whole export runs in one read-only REPEATABLE READ
transaction, so the sheets describe a single moment even while customers are
chatting (a lead cannot appear on Leads but be missing from Tasks).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.crm.excel import identity
from app.crm.excel import schema as sc
from app.crm.excel.data import DuplicateRow, ExportData, InteractionRow, LeadRow, TaskRow, ViewingRow
from app.crm.excel.text import excerpt, one_line, requirements_summary
from app.crm.repository import OPEN_LEAD_EXCLUDED
from app.database import models as db
from app.leads.qualification import is_qualified
from app.leads.scoring import score_band

logger = logging.getLogger(__name__)

_CHUNK = 5_000  # keeps "IN (...)" lists well under every driver's parameter limit
RECENT_TASK_DAYS = 30
RECENT_VIEWING_DAYS = 30
MAX_VIEWINGS = 5_000
OPEN_HANDOFF = ("open", "claimed")


def chunks(values: Sequence[int]) -> Iterator[Sequence[int]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _begin_read_only_snapshot(session: Session) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    try:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
    except InvalidRequestError:  # a transaction is already running: keep going, just not as one snapshot
        logger.warning("CRM export could not start a read-only snapshot; reading with the session's current isolation")


def _channel(customer: db.Customer, lead: db.Lead | None = None) -> str:
    if lead is not None and lead.source:
        return lead.source
    if customer.source:
        return customer.source
    return (customer.external_id or "").split(":", 1)[0]


def load_export_data(
    session: Session,
    *,
    now: datetime,
    interaction_days: int = 90,
    include_closed: bool = True,
    max_leads: int = 5_000,
    max_interactions: int = 10_000,
) -> ExportData:
    """Everything the workbook shows, read from the database as of ``now``."""
    _begin_read_only_snapshot(session)
    data = ExportData(generated_at=now, include_closed=include_closed, interaction_days=interaction_days)

    # ---- leads -----------------------------------------------------------
    lead_filter = [] if include_closed else [db.Lead.status.not_in(OPEN_LEAD_EXCLUDED)]
    total_leads = session.scalar(select(func.count()).select_from(db.Lead).where(*lead_filter)) or 0
    if total_leads > max_leads:
        data.truncated["leads"] = total_leads - max_leads

    rows = session.execute(
        select(db.Lead, db.Customer, db.Property)
        .join(db.Customer, db.Customer.id == db.Lead.customer_id)
        .outerjoin(db.Property, db.Property.property_id == db.Lead.property_ref)
        .where(*lead_filter)
        .order_by(db.Lead.id.desc())
        .limit(max_leads)
    ).all()
    seen: set[int] = set()
    joined: list[tuple[db.Lead, db.Customer, db.Property | None]] = []
    for lead, customer, prop in rows:
        if lead.id not in seen:  # a duplicated property mirror row must not duplicate a lead
            seen.add(lead.id)
            joined.append((lead, customer, prop))
    lead_ids = [lead.id for lead, _, _ in joined]

    last_message = max_by_lead(session, db.Interaction.lead_id, db.Interaction.created_at, lead_ids)
    handoff_resolved = max_by_lead(session, db.Handoff.lead_id, db.Handoff.resolved_at, lead_ids, db.Handoff.resolved_at.is_not(None))
    booking_rows = _load_bookings(session, now)
    booking_lead = resolve_booking_leads(session, booking_rows)
    next_viewing: dict[int, datetime] = {}
    for booking in booking_rows:
        lead_pk = booking_lead.get(booking.id)
        if lead_pk is not None and booking.status in sc.ACTIVE_BOOKING_STATUSES and booking.starts_at >= now:
            if lead_pk not in next_viewing or booking.starts_at < next_viewing[lead_pk]:
                next_viewing[lead_pk] = booking.starts_at
    open_handoffs = _ids_with(session, db.Handoff.lead_id, lead_ids, db.Handoff.status.in_(OPEN_HANDOFF))

    active_tasks = _load_tasks(session, lead_ids, now, include_recent_done=True)
    next_task: dict[int, str] = {}
    for task in sorted(active_tasks, key=lambda t: (t.due_at is None, t.due_at or now, t.id)):
        if task.status in sc.ACTIVE_TASK_STATUSES and task.lead_id not in next_task:
            next_task[task.lead_id] = one_line(task.description or sc.label(sc.TASK_TYPE, task.task_type), 90)

    # ---- duplicates (also decides person keys and the duplicate flag) ------
    facts, decisions, decision_rows = _load_identity(session)
    candidates = identity.find_candidates(facts)
    linked = identity.linked_pairs(candidates, decisions)
    person = identity.person_keys(linked)
    flagged = {c.a.pk for c in candidates if decisions.get((c.a.pk, c.b.pk)) != "rejected"}
    flagged |= {c.b.pk for c in candidates if decisions.get((c.a.pk, c.b.pk)) != "rejected"}
    data.duplicates = _duplicate_rows(candidates, decisions, decision_rows)

    # ---- assemble lead rows ----------------------------------------------
    leads: list[LeadRow] = []
    for lead, customer, prop in joined:
        meta = lead.metadata_json or {}
        requirements = meta.get("requirements") if isinstance(meta.get("requirements"), dict) else {}
        qualification = meta.get("qualification") if isinstance(meta.get("qualification"), dict) else {}
        score = max(0, min(100, int(lead.score or 0)))
        leads.append(
            LeadRow(
                pk=lead.id,
                uuid=str(lead.public_id),
                customer_pk=customer.id,
                name=customer.name or "",
                phone=customer.phone or "",
                phone_verified=bool(customer.phone_verified),
                email=customer.email or "",
                channel=_channel(customer, lead),
                created_at=lead.created_at,
                language=customer.preferred_language or "",
                transaction_type=requirements.get("transaction_type") or (prop.transaction_type if prop else None),
                location=lead.preferred_location or "",
                compound=(prop.compound if prop and prop.compound else ""),
                property_type=lead.preferred_property_type or "",
                budget_min=lead.budget_min,
                budget_max=lead.budget_max,
                bedrooms=lead.bedrooms_min,
                requirements=requirements_summary(requirements),
                property_ref=lead.property_ref or "",
                score=score,
                band=score_band(score),
                qualified=is_qualified(qualification),
                status=lead.status,
                assigned_agent=lead.assigned_agent or "",
                next_followup_at=lead.next_followup_at,
                last_contact_at=lead.last_contacted_at or handoff_resolved.get(lead.id),
                stored_last_contact_at=lead.last_contacted_at,
                notes=lead.notes or "",
                last_message_at=last_message.get(lead.id),
                next_viewing_at=next_viewing.get(lead.id),
                open_handoff=lead.id in open_handoffs,
                next_task=next_task.get(lead.id, ""),
                person_key=person.get(customer.id, ""),
                possible_duplicate=customer.id in flagged,
            )
        )
    leads.sort(key=lambda row: (row.status in OPEN_LEAD_EXCLUDED, -row.score, -row.pk))
    data.leads = leads

    names = {row.pk: row for row in leads}
    data.tasks = _task_rows(active_tasks, names)
    data.interactions = _load_interactions(session, leads, now, interaction_days, max_interactions, data, lead_filter)
    data.viewings = _viewing_rows(session, booking_rows, booking_lead, names)
    return data


# ---------------------------------------------------------------------------
# Per-lead aggregates
# ---------------------------------------------------------------------------



def resolve_booking_leads(session: Session, bookings: Sequence[db.Booking]) -> dict[int, int | None]:
    """``booking id -> lead id``.

    The booking flow never fills ``bookings.lead_id`` (it records the booking id
    in the lead's metadata instead), so the link is rebuilt here, read-only:
    an explicit ``lead_id`` wins; then the lead whose ``appointment_id`` is this
    booking; then the customer's latest lead created before the booking; then
    their latest lead.
    """
    result: dict[int, int | None] = {booking.id: booking.lead_id for booking in bookings if booking.lead_id is not None}
    missing = [booking for booking in bookings if booking.lead_id is None]
    if not missing:
        return result

    by_customer: dict[int, list[db.Lead]] = {}
    for chunk in chunks(sorted({booking.customer_id for booking in missing})):
        for lead in session.scalars(select(db.Lead).where(db.Lead.customer_id.in_(chunk))):
            by_customer.setdefault(lead.customer_id, []).append(lead)

    for booking in missing:
        leads = by_customer.get(booking.customer_id, [])
        exact = [lead for lead in leads if str((lead.metadata_json or {}).get("appointment_id") or "") == str(booking.public_id)]
        earlier = [lead for lead in leads if lead.created_at is not None and booking.created_at is not None and lead.created_at <= booking.created_at]
        pool = exact or earlier or leads
        result[booking.id] = max(pool, key=lambda lead: lead.id).id if pool else None
    return result


def max_by_lead(session: Session, lead_col: Any, value_col: Any, lead_ids: Sequence[int], *where: Any) -> dict[int, datetime]:
    return _aggregate(session, func.max, lead_col, value_col, lead_ids, where)


def _aggregate(session: Session, fn: Any, lead_col: Any, value_col: Any, lead_ids: Sequence[int], where: Iterable[Any]) -> dict[int, datetime]:
    result: dict[int, datetime] = {}
    for chunk in chunks(lead_ids):
        statement = select(lead_col, fn(value_col)).where(lead_col.in_(chunk), *where).group_by(lead_col)
        result.update({lead_id: value for lead_id, value in session.execute(statement) if value is not None})
    return result


def _ids_with(session: Session, lead_col: Any, lead_ids: Sequence[int], *where: Any) -> set[int]:
    found: set[int] = set()
    for chunk in chunks(lead_ids):
        found.update(session.scalars(select(lead_col).where(lead_col.in_(chunk), *where).distinct()))
    return found


# ---------------------------------------------------------------------------
# Tasks, interactions, viewings
# ---------------------------------------------------------------------------


def _load_tasks(session: Session, lead_ids: Sequence[int], now: datetime, *, include_recent_done: bool) -> list[db.CrmTask]:
    recent = now - timedelta(days=RECENT_TASK_DAYS)
    tasks: list[db.CrmTask] = []
    for chunk in chunks(lead_ids):
        condition = db.CrmTask.status.in_(sc.ACTIVE_TASK_STATUSES)
        if include_recent_done:
            condition = or_(condition, db.CrmTask.updated_at >= recent)
        tasks.extend(session.scalars(select(db.CrmTask).where(db.CrmTask.lead_id.in_(chunk), condition)))
    return tasks


def _task_rows(tasks: Sequence[db.CrmTask], leads: dict[int, LeadRow]) -> list[TaskRow]:
    rows = []
    for task in tasks:
        lead = leads.get(task.lead_id)
        rows.append(
            TaskRow(
                pk=task.id,
                uuid=str(task.public_id),
                lead_pk=task.lead_id,
                customer=lead.name if lead else "",
                phone=lead.phone if lead else "",
                task_type=task.task_type,
                description=task.description or "",
                assigned_agent=task.assigned_agent or "",
                due_at=task.due_at,
                priority=task.priority,
                status=task.status,
                completed_at=task.completed_at,
                source=task.source,
                created_at=task.created_at,
            )
        )
    # active first, then by due date (undated last), then newest
    rows.sort(key=lambda t: (t.status not in sc.ACTIVE_TASK_STATUSES, t.due_at is None, t.due_at or t.created_at, -t.pk))
    return rows


def _load_interactions(
    session: Session,
    leads: Sequence[LeadRow],
    now: datetime,
    days: int,
    limit: int,
    data: ExportData,
    lead_filter: Sequence[Any],
) -> list[InteractionRow]:
    since = now - timedelta(days=days)
    customer_ids = select(db.Lead.customer_id).where(*lead_filter)
    in_window = (db.Interaction.created_at >= since, db.Interaction.customer_id.in_(customer_ids))
    total = session.scalar(select(func.count()).select_from(db.Interaction).where(*in_window)) or 0
    if total > limit:
        data.truncated["interactions"] = total - limit

    latest_lead_of_customer: dict[int, int] = {}
    for lead in sorted(leads, key=lambda row: row.pk):  # later (newer) leads overwrite earlier ones
        latest_lead_of_customer[lead.customer_pk] = lead.pk
    exported = {lead.pk for lead in leads}

    rows = session.execute(
        select(db.Interaction, db.Customer.name)
        .join(db.Customer, db.Customer.id == db.Interaction.customer_id)
        .where(*in_window)
        .order_by(db.Interaction.created_at.desc(), db.Interaction.id.desc())
        .limit(limit)
    ).all()
    result = []
    for item, name in rows:
        lead_pk = item.lead_id if item.lead_id in exported else latest_lead_of_customer.get(item.customer_id)
        channel, _, modality = (item.channel or "").partition(":")
        result.append(
            InteractionRow(
                pk=item.id,
                lead_pk=lead_pk,
                customer=name or "",
                created_at=item.created_at,
                channel=channel,
                modality=modality or "text",
                customer_message=excerpt(item.content),
                assistant_response=excerpt(item.response),
            )
        )
    return result


def _load_bookings(session: Session, now: datetime) -> list[db.Booking]:
    recent = now - timedelta(days=RECENT_VIEWING_DAYS)
    return list(
        session.scalars(
            select(db.Booking)
            .where(or_(db.Booking.starts_at >= recent, db.Booking.status.in_(sc.ACTIVE_BOOKING_STATUSES)))
            .order_by(db.Booking.starts_at)
            .limit(MAX_VIEWINGS)
        )
    )


def _viewing_rows(
    session: Session,
    bookings: Sequence[db.Booking],
    booking_lead: dict[int, int | None],
    leads: dict[int, LeadRow],
) -> list[ViewingRow]:
    customers: dict[int, tuple[str, str]] = {}
    for chunk in chunks(sorted({booking.customer_id for booking in bookings})):
        for customer_id, name, phone in session.execute(
            select(db.Customer.id, db.Customer.name, db.Customer.phone).where(db.Customer.id.in_(chunk))
        ):
            customers[customer_id] = (name or "", phone or "")
    rows = []
    for booking in bookings:
        name, phone = customers.get(booking.customer_id, ("", ""))
        lead_pk = booking_lead.get(booking.id)
        rows.append(
            ViewingRow(
                pk=booking.id,
                lead_pk=lead_pk if lead_pk in leads else None,
                customer=name,
                phone=booking.contact_phone or phone,
                property_id=booking.property_id,
                starts_at=booking.starts_at,
                ends_at=booking.ends_at,
                status=booking.status,
                agent=booking.agent_id or "",
                notes=excerpt(booking.notes, 200),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Duplicate review
# ---------------------------------------------------------------------------


def _load_identity(
    session: Session,
) -> tuple[list[identity.CustomerFacts], dict[tuple[int, int], str], dict[tuple[int, int], db.CrmDuplicateReview]]:
    customers = session.scalars(select(db.Customer).where(or_(db.Customer.phone.is_not(None), db.Customer.email.is_not(None)))).all()
    facts = [
        identity.CustomerFacts(
            pk=customer.id,
            name=customer.name or "",
            phone=customer.phone or None,
            email=customer.email or None,
            channel=_channel(customer),
            phone_verified=bool(customer.phone_verified),
            verification_method=customer.phone_verification_method,
        )
        for customer in customers
    ]
    reviews = session.scalars(select(db.CrmDuplicateReview)).all()
    by_pair = {(review.customer_a_id, review.customer_b_id): review for review in reviews}
    return facts, {pair: review.decision for pair, review in by_pair.items()}, by_pair


def _duplicate_rows(
    candidates: Sequence[identity.Candidate],
    decisions: dict[tuple[int, int], str],
    reviews: dict[tuple[int, int], db.CrmDuplicateReview],
) -> list[DuplicateRow]:
    rows = []
    for candidate in identity.pending_candidates(candidates, decisions) + [
        c for c in candidates if decisions.get((c.a.pk, c.b.pk))
    ]:
        pair = (candidate.a.pk, candidate.b.pk)
        review = reviews.get(pair)
        rows.append(
            DuplicateRow(
                customer_a_pk=candidate.a.pk,
                customer_b_pk=candidate.b.pk,
                a_name=candidate.a.name,
                a_phone=candidate.a.phone or "",
                a_channel=candidate.a.channel,
                a_verified=identity.strongly_verified(candidate.a),
                b_name=candidate.b.name,
                b_phone=candidate.b.phone or "",
                b_channel=candidate.b.channel,
                b_verified=identity.strongly_verified(candidate.b),
                evidence=candidate.evidence,
                decision=review.decision if review else "",
                reviewed_by=review.reviewed_by or "" if review else "",
                reviewed_at=review.reviewed_at if review else None,
            )
        )
    rows.sort(key=lambda row: (row.decision != "", row.customer_a_pk, row.customer_b_pk))
    return rows
