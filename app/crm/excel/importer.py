"""Apply an import plan to PostgreSQL.

The decisions (what is valid, what conflicts) are made by :mod:`importer_core`
without a database. This module only loads the current state, applies the plan
and reports.

Safety properties
* Dry run is the default and never writes.
* Rows touched by the plan are locked (``SELECT ... FOR UPDATE``) in one fixed
  order (leads, then tasks, then reviews, ascending ids) before the state is
  read, so two imports cannot interleave and cannot deadlock each other, and
  the state the plan was made from is the state that is written to.
* Only the sales-owned columns are written. The assistant's own data (score,
  budget, requirements, qualification, metadata) is never touched, and the
  assistant's updates only set the columns they change, so neither side can
  overwrite the other.
* Each row is applied in its own savepoint: one bad row cannot undo the others
  (or, with ``all_or_nothing``, one bad row undoes all of them).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crm.excel import baseline as bl
from app.crm.excel import importer_core as core
from app.crm.excel import schema as sc
from app.crm.excel import signing
from app.crm.excel.errors import ImportRejected, WorkbookBusy, WorkbookRejected
from app.crm.excel.importer_core import Limits, local_midnight_utc
from app.crm.excel.queries import chunks, max_by_lead
from app.crm.excel.schema import bi
from app.crm.excel.text import LEAD_PREFIX, parse_code
from app.database import models as db
from app.database.session import session_scope
from app.database.upsert import insert_ignore
from app.helpers.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["CrmImportService", "ImportRejected", "ImportResult", "WorkbookBusy", "WorkbookRejected", "crm_import_service"]

MAX_AUDIT_CHANGES = 200
SHEET_OF = {"lead": sc.LEADS, "task_update": sc.TASKS, "task_create": sc.TASKS, "duplicate": sc.DUPLICATES}


@dataclass
class ImportResult:
    report: dict[str, Any]
    applied: bool
    audit_detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Loaded:
    leads: dict[str, db.Lead]  # by public_id (string)
    tasks: dict[str, db.CrmTask]  # by public_id (string)
    context: core.Context


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value).strip())
    except (ValueError, AttributeError):
        return None


class CrmImportService:
    def __init__(self, *, wait_seconds: float = 20.0) -> None:
        self._gate = threading.Semaphore(1)
        self._wait = wait_seconds

    # ------------------------------------------------------------------ public

    def run(self, blob: bytes, *, dry_run: bool = True, all_or_nothing: bool = False, now: datetime | None = None) -> ImportResult:
        settings = get_settings()
        key = signing.derive_key(settings.CRM_SIGNING_KEY, settings.ADMIN_API_KEY or settings.CRM_API_KEY)
        limits = Limits(max_bytes=settings.CRM_IMPORT_MAX_MB * 1024 * 1024, max_rows=settings.CRM_IMPORT_MAX_ROWS)
        if not self._gate.acquire(timeout=self._wait):
            raise WorkbookBusy("Another CRM import is running")
        try:
            now = now or datetime.now(timezone.utc).replace(microsecond=0)
            workbook = core.open_workbook(blob, limits)
            try:
                parsed = core.parse_workbook(workbook, key=key, limits=limits)
            finally:
                workbook.close()
            return self._run_plan(parsed, settings, now, dry_run=dry_run, all_or_nothing=all_or_nothing)
        finally:
            self._gate.release()

    # ------------------------------------------------------------------ internals

    def _run_plan(self, parsed: core.Parsed, settings: Any, now: datetime, *, dry_run: bool, all_or_nothing: bool) -> ImportResult:
        tz_name = settings.VIEWING_TIMEZONE
        with session_scope() as session:
            loaded = _load(session, parsed, now, tz_name, settings.crm_agents, lock=not dry_run)
            plan = core.plan_import(parsed, loaded.context)
            blocked = all_or_nothing and plan.has_problems
            apply_now = not dry_run and not blocked
            failure: str | None = None
            if apply_now:
                failure = _apply_all(session, plan, loaded, now, tz_name, all_or_nothing)
        applied = apply_now and failure is None

        if dry_run:
            message = "Preview only: nothing was changed. Send the same file with dry_run=false to apply the planned changes."
        elif blocked:
            message = "Nothing was applied: all_or_nothing is on and some rows have problems. Fix them (or turn it off) and try again."
        elif failure:
            message = failure
        else:
            summary = plan.summary()
            message = f"Applied {summary['fields_changed']} change(s) in {summary['applied']} row(s)."
        summary, rows = plan.summary(), plan.report_rows()
        if not applied:  # a preview must not claim anything was applied
            summary["would_apply"] = summary.pop("applied")
            for row in rows:
                row["result"] = {"applied": "would_apply", "partial": "would_apply_partly"}.get(row["result"], row["result"])
        report = {
            "dry_run": dry_run,
            "applied": applied,
            "all_or_nothing": all_or_nothing,
            "export_id": parsed.export_id,
            "exported_at": parsed.meta.header.get("generated_at"),
            "summary": summary,
            "message": message,
            "rows": rows,
        }
        logger.info("CRM import | dry_run=%s applied=%s %s", dry_run, applied, plan.summary())
        return ImportResult(report, applied, _audit_detail(plan, report))


# ---------------------------------------------------------------------------
# Loading the current state (and locking it, when we are going to write)
# ---------------------------------------------------------------------------


def _load(session: Session, parsed: core.Parsed, now: datetime, tz_name: str, roster: Sequence[str], *, lock: bool) -> _Loaded:
    tz = ZoneInfo(tz_name)
    lead_uuids = {u for u in (_uuid(r.cells["lead_uuid"]) for r in parsed.leads) if u}
    task_uuids = {u for u in (_uuid(r.cells["task_uuid"]) for r in parsed.tasks) if u}
    new_task_lead_pks = {
        pk for pk in (parse_code(LEAD_PREFIX, r.cells["lead_ref"]) for r in parsed.tasks if not str(r.cells["task_uuid"] or "").strip()) if pk
    }
    pairs = _pairs(parsed)

    # 1. find the ids (no locks yet)
    lead_ids: set[int] = set()
    for chunk in chunks(sorted(lead_uuids, key=str)):
        lead_ids.update(session.scalars(select(db.Lead.id).where(db.Lead.public_id.in_(chunk))))
    if new_task_lead_pks:  # leads that rows on the Tasks sheet point to by their Lead ID
        lead_ids.update(session.scalars(select(db.Lead.id).where(db.Lead.id.in_(sorted(new_task_lead_pks)))))
    task_ids: set[int] = set()
    for chunk in chunks(sorted(task_uuids, key=str)):
        task_ids.update(session.scalars(select(db.CrmTask.id).where(db.CrmTask.public_id.in_(chunk))))

    # 2. lock and read, always leads -> tasks -> reviews, ascending ids
    leads = _fetch(session, db.Lead, sorted(lead_ids), lock)
    tasks = _fetch(session, db.CrmTask, sorted(task_ids), lock)
    customer_ids = sorted({c for pair in pairs for c in pair})
    existing_customers = set()
    for chunk in chunks(customer_ids):
        existing_customers.update(session.scalars(select(db.Customer.id).where(db.Customer.id.in_(chunk))))
    reviews: dict[tuple[int, int], db.CrmDuplicateReview] = {}
    if pairs:
        statement = select(db.CrmDuplicateReview).where(db.CrmDuplicateReview.customer_a_id.in_(sorted({a for a, _ in pairs}))).order_by(db.CrmDuplicateReview.id)
        if lock:
            statement = statement.with_for_update()
        reviews = {(r.customer_a_id, r.customer_b_id): r for r in session.scalars(statement)}

    handoff_done = max_by_lead(session, db.Handoff.lead_id, db.Handoff.resolved_at, sorted(lead_ids), db.Handoff.resolved_at.is_not(None))
    lead_states = {
        str(lead.public_id): core.LeadState(
            pk=lead.id,
            uuid=str(lead.public_id),
            status=lead.status,
            agent=lead.assigned_agent or "",
            next_iso=bl.local_date(lead.next_followup_at, tz),
            last_iso=bl.local_date(lead.last_contacted_at or handoff_done.get(lead.id), tz),
            notes=lead.notes or "",
            closed=lead.status in sc.CLOSED_STATUSES,
        )
        for lead in leads.values()
    }
    task_states = {
        str(task.public_id): core.TaskState(
            pk=task.id, uuid=str(task.public_id), lead_pk=task.lead_id, task_type=task.task_type, description=task.description or "",
            agent=task.assigned_agent or "", due_iso=bl.local_date(task.due_at, tz), priority=task.priority, status=task.status, source=task.source,
        )
        for task in tasks.values()
    }
    duplicate_states = {}
    for a, b in pairs:
        if a < b and a in existing_customers and b in existing_customers:
            review = reviews.get((a, b))
            duplicate_states[f"{a}:{b}"] = core.DuplicateState(f"{a}:{b}", a, b, review.decision if review else "", (review.reviewed_by or "") if review else "")

    stored_agents = {name for (name,) in session.execute(select(db.Lead.assigned_agent).distinct()) if name}
    stored_agents |= {name for (name,) in session.execute(select(db.CrmTask.assigned_agent).distinct()) if name}
    known = list(roster) + sorted((name for name in stored_agents if name.casefold() not in {r.casefold() for r in roster}), key=str.casefold)

    context = core.Context(
        now=now,
        timezone=tz_name,
        agents=tuple(roster),
        known_agents=tuple(known),
        leads=lead_states,
        leads_by_pk={state.pk: state for state in lead_states.values()},
        tasks=task_states,
        duplicates=duplicate_states,
    )
    return _Loaded({str(lead.public_id): lead for lead in leads.values()}, {str(task.public_id): task for task in tasks.values()}, context)


def _fetch(session: Session, model: Any, ids: list[int], lock: bool) -> dict[int, Any]:
    rows: dict[int, Any] = {}
    for chunk in chunks(ids):
        statement = select(model).where(model.id.in_(chunk)).order_by(model.id)
        if lock:
            statement = statement.with_for_update()
        rows.update({row.id: row for row in session.scalars(statement)})
    return rows


def _pairs(parsed: core.Parsed) -> set[tuple[int, int]]:
    pairs = set()
    for row in parsed.duplicates:
        left, _, right = str(row.cells["pair_key"] or "").partition(":")
        if left.isdigit() and right.isdigit():
            pairs.add((int(left), int(right)))
    return pairs


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _apply_all(session: Session, plan: core.Plan, loaded: _Loaded, now: datetime, tz_name: str, all_or_nothing: bool) -> str | None:
    """Apply every operation. Returns a message if everything had to be undone."""
    reports = {(report.sheet, report.row): report for report in plan.reports}

    def apply_one(operation: core.Operation) -> None:
        report = reports[(SHEET_OF[operation.kind], operation.row)]
        handler = _HANDLERS[operation.kind]
        handler(session, operation, loaded, report, now, tz_name)

    if all_or_nothing:
        try:
            with session.begin_nested():
                for operation in plan.operations:
                    apply_one(operation)
        except Exception:
            logger.exception("CRM import failed; nothing was applied (all_or_nothing)")
            for report in plan.reports:
                report.changes.clear()
                report.result = "unchanged"
            return "Nothing was applied: saving failed and everything was rolled back. Try again."
        return None

    for operation in plan.operations:
        report = reports[(SHEET_OF[operation.kind], operation.row)]
        try:
            with session.begin_nested():
                apply_one(operation)
        except Exception:
            logger.exception("CRM import could not save %s row %s", report.sheet, report.row)
            report.errors.append("This row could not be saved. Try again; if it keeps failing, tell the administrator.")
            report.changes.clear()
            report.result = "error"
    return None


def _apply_lead(session: Session, op: core.Operation, loaded: _Loaded, report: core.RowReport, now: datetime, tz_name: str) -> None:
    lead = loaded.leads[op.target]
    changes = op.changes
    if "status" in changes:
        lead.status = changes["status"]
    if "assigned_agent" in changes:
        lead.assigned_agent = changes["assigned_agent"] or None
    if "last_contact" in changes:
        lead.last_contacted_at = local_midnight_utc(changes["last_contact"], tz_name)
    elif op.extra.get("auto_last_contact"):
        lead.last_contacted_at = local_midnight_utc(op.extra["auto_last_contact"], tz_name)
    if "notes" in changes:
        lead.notes = changes["notes"] or None
    if "next_followup" in changes:
        lead.next_followup_at = local_midnight_utc(changes["next_followup"], tz_name)
        _sync_followup_task(session, lead, changes["next_followup"], tz_name, now)
    if op.extra.get("cancel_system_tasks"):
        for task in session.scalars(
            select(db.CrmTask)
            .where(db.CrmTask.lead_id == lead.id, db.CrmTask.source == "system", db.CrmTask.status.in_(sc.ACTIVE_TASK_STATUSES))
            .order_by(db.CrmTask.id)
            .with_for_update()
        ):
            task.status = "cancelled"
    session.flush()


def _sync_followup_task(session: Session, lead: db.Lead, iso_date: str, tz_name: str, now: datetime) -> None:
    """One follow-up task per lead mirrors its 'next follow-up' date, so all work lives on Tasks."""
    key = f"followup:{lead.id}"
    task = session.scalar(select(db.CrmTask).where(db.CrmTask.dedupe_key == key).with_for_update())
    if not iso_date:
        if task is not None and task.status in sc.ACTIVE_TASK_STATUSES:
            task.status = "cancelled"
        return
    due = local_midnight_utc(iso_date, tz_name)
    if task is None:
        created = insert_ignore(
            session,
            db.CrmTask,
            {
                "public_id": uuid4(), "lead_id": lead.id, "task_type": "follow_up",
                "description": bi("Scheduled follow-up", "متابعة مجدولة"), "priority": "normal", "status": "open",
                "assigned_agent": lead.assigned_agent, "due_at": due, "source": "staff", "dedupe_key": key, "created_at": now, "updated_at": now,
            },
            ["dedupe_key"],
        )
        if created:
            return
        task = session.scalar(select(db.CrmTask).where(db.CrmTask.dedupe_key == key).with_for_update())
    task.due_at = due
    if task.status not in sc.ACTIVE_TASK_STATUSES:
        task.status, task.completed_at = "open", None


def _apply_task_update(session: Session, op: core.Operation, loaded: _Loaded, report: core.RowReport, now: datetime, tz_name: str) -> None:
    task = loaded.tasks[op.target]
    for name, value in op.changes.items():
        if name == "task_type":
            task.task_type = value
        elif name == "description":
            task.description = value
        elif name == "assigned_agent":
            task.assigned_agent = value or None
        elif name == "due":
            task.due_at = local_midnight_utc(value, tz_name)
        elif name == "priority":
            task.priority = value
        elif name == "status":
            task.status = value
            task.completed_at = now if value == "done" else None
    session.flush()


def _apply_task_create(session: Session, op: core.Operation, loaded: _Loaded, report: core.RowReport, now: datetime, tz_name: str) -> None:
    fields = op.changes
    created = insert_ignore(
        session,
        db.CrmTask,
        {
            "public_id": uuid4(), "lead_id": op.target, "task_type": fields["task_type"], "description": fields["description"],
            "priority": fields["priority"], "status": fields["status"], "assigned_agent": fields["assigned_agent"],
            "due_at": local_midnight_utc(fields["due"], tz_name), "completed_at": now if fields["status"] == "done" else None,
            "source": "staff", "dedupe_key": op.extra["dedupe_key"], "created_at": now, "updated_at": now,
        },
        ["dedupe_key"],
    )
    if not created:  # this file already created it
        report.changes.clear()
        report.warnings.append("This task was already created from this file, so it was not added again.")
        report.result = "duplicate"


def _apply_duplicate(session: Session, op: core.Operation, loaded: _Loaded, report: core.RowReport, now: datetime, tz_name: str) -> None:
    a, b = op.extra["a"], op.extra["b"]
    decision, reviewer = op.changes["decision"], op.changes["reviewed_by"] or None
    review = session.scalar(
        select(db.CrmDuplicateReview).where(db.CrmDuplicateReview.customer_a_id == a, db.CrmDuplicateReview.customer_b_id == b).with_for_update()
    )
    if not decision:
        if review is not None:
            session.delete(review)
    elif review is None:
        session.add(db.CrmDuplicateReview(customer_a_id=a, customer_b_id=b, decision=decision, reviewed_by=reviewer, reviewed_at=now))
    else:
        review.decision, review.reviewed_by, review.reviewed_at = decision, reviewer, now
    session.flush()


_HANDLERS = {"lead": _apply_lead, "task_update": _apply_task_update, "task_create": _apply_task_create, "duplicate": _apply_duplicate}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit_detail(plan: core.Plan, report: dict[str, Any]) -> dict[str, Any]:
    """What goes in the audit log: who/what changed, never the text of notes or customer data."""
    changes = []
    for row in plan.reports:
        for change in row.changes:
            entry = {"sheet": row.sheet, "ref": row.ref, "field": change.field}
            if change.field not in ("notes", "description"):
                entry.update({"from": change.before, "to": change.after})
            changes.append(entry)
    return {
        **report["summary"],
        "export_id": report["export_id"],
        "dry_run": report["dry_run"],
        "was_applied": report["applied"],  # not "applied": that name is the row count in the summary
        "changes": changes[:MAX_AUDIT_CHANGES],
        "changes_truncated": max(0, len(changes) - MAX_AUDIT_CHANGES),
    }


crm_import_service = CrmImportService()
