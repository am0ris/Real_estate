"""Importing an edited workbook against a real database (everything below starts from a real export)."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.crm.excel import importer as importer_module, schema as sc
from app.crm.excel.importer import ImportRejected, WorkbookRejected, crm_import_service
from app.crm.excel.importer_core import local_midnight_utc
from app.crm.excel.service import crm_workbook_service
from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_crm_excel_queries import make_lead

TZ = "Africa/Cairo"
TODAY = datetime.now(ZoneInfo(TZ)).date()
CONTACTED, LOST, QUALIFIED = (sc.LEAD_STATUS[k] for k in ("contacted", "lost", "qualified"))


def ref(pk):
    return f"L-{pk:06d}"


class Editor:
    """Edit an exported workbook the way a person would."""

    def __init__(self, blob):
        self.book = load_workbook(io.BytesIO(blob))

    def _row(self, sheet, key, value):
        ws, col = self.book[sheet], sc.column_index(sheet, key)
        return next(r for r in range(2, ws.max_row + 1) if ws.cell(row=r, column=col).value == value)

    def _set(self, sheet, where, equals, cells):
        ws, row = self.book[sheet], self._row(sheet, where, equals)
        for key, value in cells.items():
            ws.cell(row=row, column=sc.column_index(sheet, key)).value = value
        return self

    def lead(self, pk, **cells):
        return self._set(sc.LEADS, "lead_ref", ref(pk), cells)

    def task(self, pk, **cells):
        return self._set(sc.TASKS, "task_ref", f"T-{pk:06d}", cells)

    def add_task(self, **cells):
        ws = self.book[sc.TASKS]
        row = ws.max_row + 1
        for key, value in cells.items():
            ws.cell(row=row, column=sc.column_index(sc.TASKS, key)).value = value
        return self

    def duplicate(self, **cells):
        ws = self.book[sc.DUPLICATES]
        for key, value in cells.items():
            ws.cell(row=2, column=sc.column_index(sc.DUPLICATES, key)).value = value
        return self

    def bytes(self):
        out = io.BytesIO()
        self.book.save(out)
        return out.getvalue()


def export():
    return crm_workbook_service.export().content


def run(blob, **kwargs):
    return crm_import_service.run(blob, **kwargs)


def lead_of(pk):
    with session_scope() as session:
        lead = session.get(db.Lead, pk)
        return {c.name: getattr(lead, c.name) for c in db.Lead.__table__.columns}


def tasks_of(pk):
    with session_scope() as session:
        return [
            {"key": t.dedupe_key, "type": t.task_type, "status": t.status, "source": t.source, "agent": t.assigned_agent, "due": t.due_at,
             "completed": t.completed_at, "description": t.description, "priority": t.priority}
            for t in session.query(db.CrmTask).filter_by(lead_id=pk).order_by(db.CrmTask.id)
        ]


def row_report(result, sheet, ref_text):
    return next(r for r in result.report["rows"] if r["sheet"] == sheet and r["ref"] == ref_text)


def at(days):
    return datetime(TODAY.year, TODAY.month, TODAY.day) + timedelta(days=days)


@pytest.fixture(autouse=True)
def roster(settings_env):
    settings_env(CRM_AGENTS="Mona, Ahmed")


# ---------------------------------------------------------------------------


def test_a_preview_is_the_default_and_writes_nothing():
    _, lead = make_lead(name="Sara")
    before = lead_of(lead)
    result = run(Editor(export()).lead(lead, status=CONTACTED, assigned_agent="Mona").bytes())
    assert result.report["dry_run"] is True and result.applied is False
    assert result.report["summary"]["fields_changed"] == 2  # status and agent
    assert "nothing was changed" in result.report["message"]
    assert lead_of(lead) == before and tasks_of(lead) == []


def test_applying_writes_the_sales_columns_in_cairo_time():
    _, lead = make_lead()
    result = run(Editor(export()).lead(lead, status=CONTACTED, assigned_agent="mona", next_followup=at(2), notes="Called, will call back").bytes(), dry_run=False)
    assert result.applied is True and "Applied" in result.report["message"]
    row = lead_of(lead)
    assert row["status"] == "contacted" and row["assigned_agent"] == "Mona" and row["notes"] == "Called, will call back"
    assert row["next_followup_at"] == local_midnight_utc((TODAY + timedelta(days=2)).isoformat(), TZ)
    assert row["last_contacted_at"] == local_midnight_utc(TODAY.isoformat(), TZ)  # was empty, so today
    (task,) = tasks_of(lead)
    assert task["key"] == f"followup:{lead}" and task["type"] == "follow_up" and task["source"] == "staff" and task["agent"] == "Mona" and task["status"] == "open"
    assert task["due"] == row["next_followup_at"]


def test_moving_or_clearing_the_follow_up_date_keeps_a_single_task():
    _, lead = make_lead()
    run(Editor(export()).lead(lead, next_followup=at(2)).bytes(), dry_run=False)
    run(Editor(export()).lead(lead, next_followup=at(5)).bytes(), dry_run=False)
    (task,) = tasks_of(lead)
    assert task["due"] == local_midnight_utc((TODAY + timedelta(days=5)).isoformat(), TZ)

    run(Editor(export()).lead(lead, next_followup=None).bytes(), dry_run=False)
    assert [t["status"] for t in tasks_of(lead)] == ["cancelled"] and lead_of(lead)["next_followup_at"] is None

    run(Editor(export()).lead(lead, next_followup=at(7)).bytes(), dry_run=False)
    assert [t["status"] for t in tasks_of(lead)] == ["open"]  # the same task is reopened, not duplicated


def test_closing_a_lead_cancels_its_system_tasks_and_leaves_staff_tasks_alone():
    customer, lead = make_lead()
    with session_scope() as session:
        session.add(db.Handoff(customer_id=customer, lead_id=lead, status="open", reason="wants a person", priority="high"))
        session.add(db.CrmTask(lead_id=lead, task_type="send_details", source="staff", description="mine"))
    blob = export()  # the export also creates the system task for the handoff
    run(Editor(blob).lead(lead, status=LOST).bytes(), dry_run=False)
    assert {t["source"]: t["status"] for t in tasks_of(lead)} == {"system": "cancelled", "staff": "open"}
    assert lead_of(lead)["status"] == "lost"


def test_a_field_the_assistant_changed_after_the_export_is_not_overwritten():
    _, lead = make_lead()
    blob = export()
    with session_scope() as session:  # the assistant promotes the lead while the file is open in Excel
        session.get(db.Lead, lead).status = "hot"
    result = run(Editor(blob).lead(lead, status=CONTACTED, assigned_agent="Mona").bytes(), dry_run=False)
    row = row_report(result, sc.LEADS, ref(lead))
    assert row["result"] == "partial" and row["conflicts"][0]["field"] == "status"
    assert lead_of(lead)["status"] == "hot" and lead_of(lead)["assigned_agent"] == "Mona"  # only the conflicting field was held back


def test_a_note_the_assistant_appended_after_the_export_is_never_lost():
    _, lead = make_lead()
    blob = export()
    with session_scope() as session:
        session.get(db.Lead, lead).notes = "[2026-11-10 09:00] customer asked for a viewing"
    result = run(Editor(blob).lead(lead, notes="my own note").bytes(), dry_run=False)
    assert row_report(result, sc.LEADS, ref(lead))["conflicts"][0]["field"] == "notes"
    assert lead_of(lead)["notes"] == "[2026-11-10 09:00] customer asked for a viewing"


def test_uploading_the_same_file_twice_changes_nothing_the_second_time():
    _, lead = make_lead()
    blob = Editor(export()).lead(lead, status=CONTACTED, assigned_agent="Mona").add_task(
        lead_ref=ref(lead), task_type=sc.TASK_TYPE["send_details"], description="Send plans", due=at(1)).bytes()
    first = run(blob, dry_run=False)
    snapshot = (lead_of(lead), tasks_of(lead))
    second = run(blob, dry_run=False)
    assert first.report["summary"]["fields_changed"] == 3 and second.report["summary"]["fields_changed"] == 0
    assert (lead_of(lead), tasks_of(lead)) == snapshot
    assert [r["result"] for r in second.report["rows"] if r["sheet"] == sc.TASKS] == ["duplicate"]


def test_a_new_task_row_creates_a_staff_task():
    _, lead = make_lead()
    run(Editor(export()).add_task(lead_ref=ref(lead), task_type=sc.TASK_TYPE["send_details"], description="Send the payment plan", assigned_agent="ahmed",
                                  due=at(3), priority=sc.PRIORITY["urgent"]).bytes(), dry_run=False)
    (task,) = tasks_of(lead)
    assert task["source"] == "staff" and task["key"].startswith("staff:") and task["agent"] == "Ahmed" and task["priority"] == "urgent"
    assert task["status"] == "open" and task["due"] == local_midnight_utc((TODAY + timedelta(days=3)).isoformat(), TZ)


def test_completing_and_reopening_a_task_sets_and_clears_the_completion_time():
    customer, lead = make_lead()
    with session_scope() as session:
        session.add(db.CrmTask(lead_id=lead, task_type="call_customer", description="Call", source="staff"))
    with session_scope() as session:
        task_pk = session.query(db.CrmTask).one().id
    run(Editor(export()).task(task_pk, status=sc.TASK_STATUS["done"]).bytes(), dry_run=False)
    assert tasks_of(lead)[0]["status"] == "done" and tasks_of(lead)[0]["completed"] is not None
    run(Editor(export()).task(task_pk, status=sc.TASK_STATUS["open"]).bytes(), dry_run=False)
    assert tasks_of(lead)[0]["status"] == "open" and tasks_of(lead)[0]["completed"] is None


def test_duplicate_decisions_are_stored_grouped_and_can_be_withdrawn():
    phone = "+201012345678"
    first, lead_a = make_lead("web:one", phone=phone, name="Web visitor")
    second, lead_b = make_lead("whatsapp:201012345678", phone=phone, source="whatsapp", name="WA sender")
    low, high = sorted((first, second))

    run(Editor(export()).duplicate(decision=sc.DUPLICATE_DECISION["confirmed"], reviewed_by="Mona").bytes(), dry_run=False)
    with session_scope() as session:
        review = session.query(db.CrmDuplicateReview).one()
        assert (review.customer_a_id, review.customer_b_id, review.decision, review.reviewed_by) == (low, high, "confirmed", "Mona")
    person_column = get_column_letter(sc.column_index(sc.LEADS, "person"))
    persons = {c.value for c in load_workbook(io.BytesIO(export()))[sc.LEADS][person_column][1:] if c.value}
    assert persons == {f"P-{low:06d}"}

    run(Editor(export()).duplicate(decision=sc.DUPLICATE_DECISION[""]).bytes(), dry_run=False)
    with session_scope() as session:
        assert session.query(db.CrmDuplicateReview).count() == 0


def test_only_the_sales_columns_are_ever_written():
    _, lead = make_lead(score=80, budget_max=9_000_000, preferred_location="New Cairo", metadata_json={"requirements": {"district": "New Cairo"}, "qualification": {"budget": 9}})
    before = lead_of(lead)
    run(Editor(export()).lead(lead, status=CONTACTED, assigned_agent="Mona", notes="n", next_followup=at(1), last_contact=TODAY).bytes(), dry_run=False)
    after = lead_of(lead)
    changed = {k for k in before if before[k] != after[k]}
    assert changed <= {"status", "assigned_agent", "notes", "next_followup_at", "last_contacted_at", "updated_at"}
    assert after["score"] == 80 and after["metadata_json"] == before["metadata_json"] and after["budget_max"] == before["budget_max"]


def test_all_or_nothing_applies_nothing_when_any_row_is_wrong():
    _, good = make_lead()
    _, bad = make_lead()
    blob = Editor(export()).lead(good, assigned_agent="Mona").lead(bad, status=sc.LEAD_STATUS["hot"]).bytes()  # "hot" cannot be chosen by hand
    blocked = run(blob, dry_run=False, all_or_nothing=True)
    assert blocked.applied is False and "all_or_nothing" in blocked.report["message"] and lead_of(good)["assigned_agent"] is None
    partial = run(blob, dry_run=False)
    assert partial.applied is True and lead_of(good)["assigned_agent"] == "Mona" and lead_of(bad)["status"] == "new"
    assert row_report(partial, sc.LEADS, ref(bad))["result"] == "error"


def test_a_row_that_cannot_be_saved_does_not_stop_the_others(monkeypatch):
    _, first = make_lead()
    _, second = make_lead()
    original = importer_module._HANDLERS["lead"]
    with session_scope() as session:
        doomed = str(session.get(db.Lead, first).public_id)

    def flaky(session, op, loaded, report, now, tz_name):
        if op.target == doomed:
            raise RuntimeError("simulated database error")
        return original(session, op, loaded, report, now, tz_name)

    monkeypatch.setitem(importer_module._HANDLERS, "lead", flaky)
    result = run(Editor(export()).lead(first, assigned_agent="Mona").lead(second, assigned_agent="Ahmed").bytes(), dry_run=False)
    assert row_report(result, sc.LEADS, ref(first))["result"] == "error" and lead_of(first)["assigned_agent"] is None
    assert lead_of(second)["assigned_agent"] == "Ahmed"


def test_agents_outside_the_roster_are_refused():
    _, lead = make_lead()
    result = run(Editor(export()).lead(lead, assigned_agent="Stranger").bytes(), dry_run=False)
    assert "not in the agents list" in row_report(result, sc.LEADS, ref(lead))["errors"][0] and lead_of(lead)["assigned_agent"] is None


def test_bad_files_are_rejected_before_anything_is_read(settings_env):
    _, lead = make_lead()
    blob = export()
    with pytest.raises(ImportRejected):
        run(b"this is not a workbook")
    book = load_workbook(io.BytesIO(blob))
    book["_meta"].cell(row=8, column=3).value = '{"status":"won"}'  # the first baseline row
    out = io.BytesIO(); book.save(out)
    with pytest.raises(WorkbookRejected, match="signature"):
        run(out.getvalue(), dry_run=False)
    settings_env(ADMIN_API_KEY="a-completely-different-admin-key-0123456789")  # a file from another deployment
    with pytest.raises(WorkbookRejected, match="signature"):
        run(blob, dry_run=False)


def test_the_audit_detail_lists_changes_but_never_the_text_of_notes():
    _, lead = make_lead()
    result = run(Editor(export()).lead(lead, status=CONTACTED, notes="customer's private details: 0100 123 4567").bytes(), dry_run=False)
    detail = result.audit_detail
    assert detail["was_applied"] is True and detail["fields_changed"] >= 2
    fields = {c["field"]: c for c in detail["changes"]}
    assert fields["status"]["to"] == sc.LEAD_STATUS["contacted"] and "from" not in fields["notes"] and "to" not in fields["notes"]
    assert "0100" not in str(detail)
