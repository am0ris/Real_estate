"""Reading an edited workbook back: safety checks, per-field merge rules and validation.

Every test starts from a workbook produced by the real exporter, edits cells the way a
person would, and checks the plan. No database is involved: the 'database now' is a
:class:`Context` built from the same data, which tests change to simulate someone
else editing after the export.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from app.crm.excel import importer_core as core, schema as sc, signing
from app.crm.excel.baseline import local_date
from app.crm.excel.data import DuplicateRow, ExportData, LeadRow, TaskRow
from app.crm.excel.exporter import build_workbook
from app.crm.excel.meta import WorkbookRejected
from zoneinfo import ZoneInfo

TZ = "Africa/Cairo"
KEY = signing.derive_key(None, "unit-test-admin-key-0123456789")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
TODAY = "2026-11-10"
LIMITS = core.Limits()


def lead(pk, **kw):
    base = dict(pk=pk, uuid=f"u-lead-{pk}", customer_pk=pk, name=f"Customer {pk}", status="new", created_at=NOW - timedelta(days=3))
    base.update(kw)
    return LeadRow(**base)


def task(pk, lead_pk=1, **kw):
    base = dict(pk=pk, uuid=f"u-task-{pk}", lead_pk=lead_pk, customer="C", task_type="call_customer", description="Call back",
                priority="high", status="open", source="system", created_at=NOW, due_at=NOW + timedelta(days=1))
    base.update(kw)
    return TaskRow(**base)


def data():
    return ExportData(
        generated_at=NOW,
        leads=[lead(1, status="new"), lead(2, status="hot", assigned_agent="Mona", notes="line one"), lead(3, status="contacted", last_contact_at=NOW - timedelta(days=4)),
               lead(4, status="won"), lead(5, status="qualified", next_followup_at=NOW + timedelta(days=2))],
        tasks=[task(1), task(2, 2, task_type="send_details", description="Send plans", status="in_progress", assigned_agent="Ahmed")],
        duplicates=[DuplicateRow(1, 2, "A", "+201", "website", False, "B", "+201", "whatsapp", True, "same_phone")],
    )


def export(d=None, agents=("Mona", "Ahmed")):
    return build_workbook(d or data(), agents=list(agents), timezone=TZ, signing_key=KEY)


def context(d=None, agents=("Mona", "Ahmed"), **overrides):
    d = d or data()
    tz = ZoneInfo(TZ)
    leads = {
        l.uuid: core.LeadState(l.pk, l.uuid, l.status, l.assigned_agent, local_date(l.next_followup_at, tz), local_date(l.last_contact_at, tz), l.notes, l.status in sc.CLOSED_STATUSES)
        for l in d.leads
    }
    tasks = {
        t.uuid: core.TaskState(t.pk, t.uuid, t.lead_pk, t.task_type, t.description, t.assigned_agent, local_date(t.due_at, tz), t.priority, t.status, t.source)
        for t in d.tasks
    }
    dups = {x.pair_key: core.DuplicateState(x.pair_key, x.customer_a_pk, x.customer_b_pk, x.decision, x.reviewed_by) for x in d.duplicates}
    known = list(agents) + sorted({v for v in [l.assigned_agent for l in d.leads] + [t.assigned_agent for t in d.tasks] if v and v.casefold() not in {a.casefold() for a in agents}})
    ctx = core.Context(NOW, TZ, tuple(agents), tuple(known), leads, {s.pk: s for s in leads.values()}, tasks, dups)
    return replace(ctx, **overrides) if overrides else ctx


class Sheet:
    """Edit an exported workbook like a person: find a row by id, set cells by column key."""

    def __init__(self, blob):
        self.book = load_workbook(io.BytesIO(blob))

    def row(self, sheet, key, value):
        ws, col = self.book[sheet], sc.column_index(sheet, key)
        return next(r for r in range(2, ws.max_row + 1) if ws.cell(row=r, column=col).value == value)

    def set(self, sheet, key, value, *, where, equals):
        ws = self.book[sheet]
        ws.cell(row=self.row(sheet, where, equals), column=sc.column_index(sheet, key)).value = value
        return self

    def lead(self, pk, **cells):
        for key, value in cells.items():
            self.set(sc.LEADS, key, value, where="lead_uuid", equals=f"u-lead-{pk}")
        return self

    def task(self, pk, **cells):
        for key, value in cells.items():
            self.set(sc.TASKS, key, value, where="task_uuid", equals=f"u-task-{pk}")
        return self

    def add_task(self, **cells):
        ws = self.book[sc.TASKS]
        row = ws.max_row + 1
        for key, value in cells.items():
            ws.cell(row=row, column=sc.column_index(sc.TASKS, key)).value = value
        return self

    def dup(self, **cells):
        for key, value in cells.items():
            self.set(sc.DUPLICATES, key, value, where="pair_key", equals="1:2")
        return self

    def bytes(self):
        buffer = io.BytesIO()
        self.book.save(buffer)
        return buffer.getvalue()


def plan_for(blob, ctx=None, key=KEY):
    parsed = core.parse_workbook(core.open_workbook(blob, LIMITS), key=key, limits=LIMITS)
    return core.plan_import(parsed, ctx or context())


def ops(plan, kind=None):
    return [o for o in plan.operations if kind is None or o.kind == kind]


def report(plan, sheet, ref):
    return next(r for r in plan.reports if r.sheet == sheet and r.ref == ref)


L = lambda pk: f"L-{pk:06d}"  # noqa: E731
CONTACTED, QUALIFIED, NEGOTIATION, WON, LOST = (sc.LEAD_STATUS[k] for k in ("contacted", "qualified", "negotiation", "won", "lost"))


# ---------------------------------------------------------------------------
# An untouched file changes nothing
# ---------------------------------------------------------------------------


def test_an_unedited_export_changes_nothing_and_raises_no_complaints():
    plan = plan_for(export())
    assert plan.operations == [] and not plan.has_problems
    assert plan.summary()["rows_read"] == 5 + 2 + 1 and plan.summary()["applied"] == 0
    assert plan.report_rows() == []


def test_a_status_the_system_does_not_know_is_not_an_edit():
    d = data()
    d.leads.append(lead(9, status="legacy_state"))
    assert not plan_for(export(d), context(d)).has_problems


def test_sorting_rows_and_reordering_columns_does_not_matter():
    book = load_workbook(io.BytesIO(export()))
    ws = book[sc.LEADS]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=1, max_row=ws.max_row)]
    header, body = rows[0], rows[1:][::-1]  # newest first
    order = list(range(len(header)))[::-1]  # and the columns mirrored
    for r, values in enumerate([header] + body, start=1):
        for c, source in enumerate(order, start=1):
            ws.cell(row=r, column=c).value = values[source]
    out = io.BytesIO(); book.save(out)
    assert not plan_for(out.getvalue()).has_problems and plan_for(out.getvalue()).operations == []


# ---------------------------------------------------------------------------
# Lead status
# ---------------------------------------------------------------------------


def test_a_valid_status_change_is_planned_and_fills_an_empty_last_contact():
    plan = plan_for(Sheet(export()).lead(1, status=CONTACTED).bytes())
    (op,) = ops(plan, "lead")
    assert op.changes == {"status": "contacted"} and op.extra == {"auto_last_contact": TODAY}
    assert report(plan, sc.LEADS, L(1)).result == "applied" and any("set to today" in w for w in report(plan, sc.LEADS, L(1)).warnings)


def test_a_status_change_keeps_an_existing_last_contact():
    plan = plan_for(Sheet(export()).lead(3, status=NEGOTIATION).bytes())
    assert ops(plan, "lead")[0].extra == {}


@pytest.mark.parametrize("pk,target,message", [
    (1, sc.LEAD_STATUS["hot"], "set by the system"),
    (1, sc.LEAD_STATUS["viewing_booked"], "set by the system"),
    (1, WON, "cannot go from"),
    (1, NEGOTIATION, "cannot go from"),
    (4, CONTACTED, "cannot go from"),
    (2, "nonsense", "not a valid status"),
    (2, None, "not a valid status"),
])
def test_forbidden_or_unknown_statuses_are_rejected_with_a_reason(pk, target, message):
    plan = plan_for(Sheet(export()).lead(pk, status=target).bytes())
    assert ops(plan) == [] and any(message in e for e in report(plan, sc.LEADS, L(pk)).errors)


def test_closing_a_lead_asks_for_its_system_tasks_to_be_cancelled():
    plan = plan_for(Sheet(export()).lead(3, status=LOST).bytes())
    assert ops(plan, "lead")[0].extra["cancel_system_tasks"] is True


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_agents_are_matched_without_case_and_stored_in_the_rosters_spelling():
    plan = plan_for(Sheet(export()).lead(1, assigned_agent="  ahmed ").bytes())
    assert ops(plan, "lead")[0].changes == {"assigned_agent": "Ahmed"}


def test_an_agent_outside_the_roster_is_rejected_and_clearing_is_allowed():
    bad = plan_for(Sheet(export()).lead(1, assigned_agent="Stranger").bytes())
    assert ops(bad) == [] and "not in the agents list" in report(bad, sc.LEADS, L(1)).errors[0]
    cleared = plan_for(Sheet(export()).lead(2, assigned_agent=None).bytes())
    assert ops(cleared, "lead")[0].changes == {"assigned_agent": ""}


def test_anyone_can_be_assigned_when_no_roster_is_configured():
    ctx = context(agents=())
    plan = plan_for(Sheet(export(agents=())).lead(1, assigned_agent="Anyone At All").bytes(), ctx)
    assert ops(plan, "lead")[0].changes == {"assigned_agent": "Anyone At All"}


def test_someone_already_assigned_in_the_database_is_still_valid():
    d = data()  # lead 2 -> Mona, task 2 -> Ahmed; add a name that is not on the roster
    d.leads[1] = replace(d.leads[1], assigned_agent="Former Agent")
    plan = plan_for(Sheet(export(d)).lead(1, assigned_agent="Former Agent").bytes(), context(d))
    assert ops(plan, "lead")[0].changes == {"assigned_agent": "Former Agent"}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed,expected", [
    (datetime(2026, 12, 31), "2026-12-31"), ("2026-12-31", "2026-12-31"), ("31/12/2026", "2026-12-31"), ("31-12-2026", "2026-12-31"),
    (46387, "2026-12-31"),  # an Excel date serial typed into a General cell
])
def test_dates_are_accepted_in_every_form_excel_produces(typed, expected):
    plan = plan_for(Sheet(export()).lead(1, next_followup=typed).bytes())
    assert ops(plan, "lead")[0].changes == {"next_followup": expected}


@pytest.mark.parametrize("typed", ["soon", "2026-02-30", "13/13/2026", 1, "1999-01-01", datetime(2101, 1, 1)])
def test_unreadable_or_out_of_range_dates_are_rejected(typed):
    plan = plan_for(Sheet(export()).lead(1, next_followup=typed).bytes())
    assert ops(plan) == [] and report(plan, sc.LEADS, L(1)).errors


def test_a_follow_up_in_the_past_is_allowed_with_a_warning_and_clearing_it_is_allowed():
    past = plan_for(Sheet(export()).lead(1, next_followup=datetime(2026, 11, 1)).bytes())
    assert ops(past, "lead")[0].changes == {"next_followup": "2026-11-01"} and "overdue" in report(past, sc.LEADS, L(1)).warnings[0]
    cleared = plan_for(Sheet(export()).lead(5, next_followup=None).bytes())
    assert ops(cleared, "lead")[0].changes == {"next_followup": ""}


def test_a_last_contact_date_cannot_be_in_the_future():
    plan = plan_for(Sheet(export()).lead(1, last_contact=datetime(2026, 11, 11)).bytes())
    assert ops(plan) == [] and "future" in report(plan, sc.LEADS, L(1)).errors[0]
    assert ops(plan_for(Sheet(export()).lead(1, last_contact=datetime(2026, 11, 10)).bytes()), "lead")  # today is fine


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def test_notes_can_be_edited_and_cleared():
    edited = plan_for(Sheet(export()).lead(2, notes="line one\n[2026-11-10] called, no answer").bytes())
    assert ops(edited, "lead")[0].changes == {"notes": "line one\n[2026-11-10] called, no answer"}
    assert ops(plan_for(Sheet(export()).lead(2, notes=None).bytes()), "lead")[0].changes == {"notes": ""}


def test_notes_too_long_for_a_cell_are_shown_cut_and_never_overwritten():
    d = data()
    d.leads[0] = replace(d.leads[0], notes="n" * (core.CELL_LIMIT + 800))
    plan = plan_for(Sheet(export(d)).lead(1, notes="my replacement").bytes(), context(d))
    assert ops(plan) == [] and "too long to edit" in report(plan, sc.LEADS, L(1)).warnings[0]


# ---------------------------------------------------------------------------
# The three-way rule
# ---------------------------------------------------------------------------


def test_a_field_changed_by_someone_else_since_the_export_is_a_conflict_and_is_not_written():
    ctx = context()
    changed = dict(ctx.leads)
    changed["u-lead-1"] = replace(changed["u-lead-1"], status="hot")  # the assistant promoted the lead after the export
    ctx = replace(ctx, leads=changed, leads_by_pk={s.pk: s for s in changed.values()})
    plan = plan_for(Sheet(export()).lead(1, status=QUALIFIED, assigned_agent="Mona").bytes(), ctx)
    r = report(plan, sc.LEADS, L(1))
    assert r.conflicts == [{"field": "status", "yours": QUALIFIED, "database": sc.LEAD_STATUS["hot"]}]
    assert ops(plan, "lead")[0].changes == {"assigned_agent": "Mona"} and r.result == "partial"  # the other edit still goes through


def test_a_note_the_assistant_appended_after_the_export_is_never_overwritten():
    ctx = context()
    changed = dict(ctx.leads)
    changed["u-lead-2"] = replace(changed["u-lead-2"], notes="line one\n[2026-11-10 09:00] customer asked for a viewing")
    ctx = replace(ctx, leads=changed, leads_by_pk={s.pk: s for s in changed.values()})
    plan = plan_for(Sheet(export()).lead(2, notes="line one\nmy own note").bytes(), ctx)
    assert ops(plan) == [] and report(plan, sc.LEADS, L(2)).conflicts[0]["field"] == "notes"
    assert "viewing" in report(plan, sc.LEADS, L(2)).conflicts[0]["database"]


def test_fields_the_person_did_not_touch_are_left_alone_even_if_the_data_moved():
    ctx = context()
    changed = dict(ctx.leads)
    changed["u-lead-2"] = replace(changed["u-lead-2"], agent="Ahmed", status="contacted")
    ctx = replace(ctx, leads=changed, leads_by_pk={s.pk: s for s in changed.values()})
    plan = plan_for(Sheet(export()).lead(2, notes="just a note").bytes(), ctx)
    assert ops(plan, "lead")[0].changes == {"notes": "just a note"} and not plan.has_problems


def test_a_value_already_in_the_database_is_a_quiet_no_op_so_a_file_can_be_uploaded_twice():
    ctx = context()
    changed = dict(ctx.leads)
    changed["u-lead-1"] = replace(changed["u-lead-1"], status="contacted", last_iso="2026-11-10")
    ctx = replace(ctx, leads=changed, leads_by_pk={s.pk: s for s in changed.values()})
    plan = plan_for(Sheet(export()).lead(1, status=CONTACTED).bytes(), ctx)
    assert ops(plan) == [] and not plan.has_problems and report(plan, sc.LEADS, L(1)).result == "unchanged"


def test_rows_that_are_not_in_the_snapshot_or_no_longer_exist_are_reported_not_guessed():
    foreign = Sheet(export()).lead(1, status=CONTACTED, lead_uuid="u-lead-999").bytes()  # id changed last
    assert "not part of the exported snapshot" in report(plan_for(foreign), sc.LEADS, L(1)).errors[0]
    ctx = context()
    gone = {k: v for k, v in ctx.leads.items() if k != "u-lead-1"}
    plan = plan_for(Sheet(export()).lead(1, status=CONTACTED).bytes(), replace(ctx, leads=gone))
    assert "no longer exists" in report(plan, sc.LEADS, L(1)).errors[0]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_task_fields_can_be_edited():
    sheet = Sheet(export()).task(1, status=sc.TASK_STATUS["done"], priority=sc.PRIORITY["low"], assigned_agent="mona", description="Called, left a voicemail",
                                 task_type=sc.TASK_TYPE["follow_up"], due=datetime(2026, 11, 20))
    (op,) = ops(plan_for(sheet.bytes()), "task_update")
    assert op.changes == {"status": "done", "priority": "low", "assigned_agent": "Mona", "description": "Called, left a voicemail",
                          "task_type": "follow_up", "due": "2026-11-20"}


@pytest.mark.parametrize("cells,message", [
    ({"status": "finished"}, "status"), ({"priority": "asap"}, "priority"), ({"task_type": "dance"}, "task_type"),
    ({"description": None}, "cannot be empty"), ({"description": "x" * 501}, "too long"), ({"due": "tomorrow"}, "due"), ({"assigned_agent": "Stranger"}, "agents list"),
])
def test_invalid_task_edits_are_rejected(cells, message):
    plan = plan_for(Sheet(export()).task(1, **cells).bytes())
    assert ops(plan) == [] and any(message in e for e in report(plan, sc.TASKS, "T-000001").errors)


def test_a_task_changed_by_someone_else_is_a_conflict():
    ctx = context()
    changed = dict(ctx.tasks)
    changed["u-task-1"] = replace(changed["u-task-1"], status="cancelled")
    plan = plan_for(Sheet(export()).task(1, status=sc.TASK_STATUS["done"]).bytes(), replace(ctx, tasks=changed))
    assert ops(plan) == [] and report(plan, sc.TASKS, "T-000001").conflicts[0]["field"] == "status"


def test_a_new_task_row_creates_a_task_for_the_lead():
    plan = plan_for(Sheet(export()).add_task(lead_ref=L(2), task_type=sc.TASK_TYPE["send_details"], description="Send the payment plan", due=datetime(2026, 11, 15)).bytes())
    (op,) = ops(plan, "task_create")
    assert op.target == 2 and op.changes["task_type"] == "send_details" and op.changes["priority"] == "normal" and op.changes["status"] == "open"
    assert op.changes["due"] == "2026-11-15" and op.extra["dedupe_key"].startswith("staff:")


def test_the_same_new_task_from_the_same_file_always_has_the_same_key():
    blob = Sheet(export()).add_task(lead_ref=L(2), task_type=sc.TASK_TYPE["other"], description="Same thing").bytes()
    first, second = (ops(plan_for(blob), "task_create")[0].extra["dedupe_key"] for _ in range(2))
    other_file = Sheet(export()).add_task(lead_ref=L(2), task_type=sc.TASK_TYPE["other"], description="Same thing").bytes()
    assert first == second and first != ops(plan_for(other_file), "task_create")[0].extra["dedupe_key"]  # a different export is a different task


@pytest.mark.parametrize("cells,message", [
    ({"lead_ref": "L-000999"}, "was not found"), ({"lead_ref": "nonsense"}, "was not found"), ({"lead_ref": L(4)}, "closed"),
    ({"task_type": None}, "task type"), ({"description": None}, "description"), ({"due": "later"}, "not a date"), ({"priority": "asap"}, "priority"),
])
def test_an_incomplete_new_task_is_rejected(cells, message):
    row = dict(lead_ref=L(2), task_type=sc.TASK_TYPE["other"], description="Do it")
    row.update(cells)
    plan = plan_for(Sheet(export()).add_task(**row).bytes())
    assert ops(plan) == [] and any(message in e for e in report(plan, sc.TASKS, "(new)").errors)


# ---------------------------------------------------------------------------
# Duplicate review
# ---------------------------------------------------------------------------


def test_a_duplicate_decision_is_planned_with_the_reviewer():
    plan = plan_for(Sheet(export()).dup(decision=sc.DUPLICATE_DECISION["confirmed"], reviewed_by="Mona").bytes())
    (op,) = ops(plan, "duplicate")
    assert op.changes == {"decision": "confirmed", "reviewed_by": "Mona"} and op.extra == {"a": 1, "b": 2}


def test_a_decision_can_be_withdrawn_and_bad_values_are_rejected():
    ctx = context()
    decided = {"1:2": core.DuplicateState("1:2", 1, 2, "confirmed", "Mona")}
    d = data()
    d.duplicates[0] = replace(d.duplicates[0], decision="confirmed", reviewed_by="Mona")
    withdrawn = plan_for(Sheet(export(d)).dup(decision=sc.DUPLICATE_DECISION[""]).bytes(), replace(ctx, duplicates=decided))
    assert ops(withdrawn, "duplicate")[0].changes["decision"] == ""
    bad = plan_for(Sheet(export()).dup(decision="perhaps").bytes())
    assert ops(bad) == [] and "not valid" in report(bad, sc.DUPLICATES, "1:2").errors[0]


def test_a_decision_someone_else_made_since_the_export_is_a_conflict():
    ctx = context()
    plan = plan_for(Sheet(export()).dup(decision=sc.DUPLICATE_DECISION["confirmed"]).bytes(),
                    replace(ctx, duplicates={"1:2": core.DuplicateState("1:2", 1, 2, "rejected", "Ahmed")}))
    assert ops(plan) == [] and report(plan, sc.DUPLICATES, "1:2").conflicts[0]["database"] == "rejected"


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_a_workbook_from_another_deployment_or_with_a_tampered_baseline_is_rejected():
    with pytest.raises(WorkbookRejected, match="signature"):
        plan_for(export(), key=signing.derive_key(None, "another-deployments-admin-key-0123456"))
    book = load_workbook(io.BytesIO(export()))
    book["_meta"].cell(row=9, column=3).value = '{"status":"won"}'
    out = io.BytesIO(); book.save(out)
    with pytest.raises(WorkbookRejected, match="signature"):
        plan_for(out.getvalue())


def test_a_plain_workbook_that_was_never_exported_is_rejected():
    from openpyxl import Workbook

    out = io.BytesIO(); Workbook().save(out)
    with pytest.raises(WorkbookRejected, match="_meta"):
        plan_for(out.getvalue())


def test_renamed_or_deleted_columns_are_reported_by_name():
    book = load_workbook(io.BytesIO(export()))
    book[sc.LEADS].cell(row=1, column=sc.column_index(sc.LEADS, "status")).value = "Stage"
    out = io.BytesIO(); book.save(out)
    with pytest.raises(core.ImportRejected, match="Lead status"):
        plan_for(out.getvalue())


def test_a_deleted_sheet_is_reported():
    book = load_workbook(io.BytesIO(export()))
    del book[sc.TASKS]
    out = io.BytesIO(); book.save(out)
    with pytest.raises(core.ImportRejected, match="Tasks"):
        plan_for(out.getvalue())


@pytest.mark.parametrize("blob,message", [
    (b"", "empty"), (b"not a spreadsheet at all", "not an .xlsx"), (b"PK\x03\x04garbage", "damaged"),
])
def test_files_that_are_not_workbooks_are_refused(blob, message):
    with pytest.raises(core.ImportRejected, match=message):
        core.open_workbook(blob, LIMITS)


def test_oversized_uploads_and_zip_bombs_are_refused():
    with pytest.raises(core.ImportRejected, match="larger than"):
        core.open_workbook(export(), core.Limits(max_bytes=1000))
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * 60_000_000)
    with pytest.raises(core.ImportRejected, match="unreasonable size"):
        core.open_workbook(bomb.getvalue(), LIMITS)


@pytest.mark.parametrize("part", ["xl/vbaProject.bin", "xl/externalLinks/externalLink1.xml", "xl/embeddings/oleObject1.bin"])
def test_macros_external_links_and_embedded_objects_are_refused(part):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(export())) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(part, b"x")
    with pytest.raises(core.ImportRejected, match="not accepted"):
        core.open_workbook(out.getvalue(), LIMITS)


def test_too_many_rows_are_refused():
    many = ExportData(generated_at=NOW, leads=[lead(i) for i in range(1, 60)])
    with pytest.raises(core.ImportRejected, match="more than 20 rows"):
        parsed_limits = core.Limits(max_rows=20)
        core.parse_workbook(core.open_workbook(export(many), parsed_limits), key=KEY, limits=parsed_limits)


def test_the_report_lists_only_rows_that_need_attention_and_counts_everything():
    sheet = Sheet(export()).lead(1, status=CONTACTED).lead(2, status="nonsense").lead(5, next_followup=datetime(2026, 11, 1))
    plan = plan_for(sheet.bytes())
    summary = plan.summary()
    assert summary["fields_changed"] == 2 and summary["errors"] == 1 and summary["warnings"] == 2 and summary["applied"] == 2
    assert {r["ref"] for r in plan.report_rows()} == {L(1), L(2), L(5)}
    assert plan.has_problems


def test_local_midnight_is_converted_to_the_utc_moment_the_database_stores():
    assert core.local_midnight_utc("2026-11-10", TZ) == datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)  # Cairo is UTC+2 in November
    assert core.local_midnight_utc("2026-07-10", TZ) == datetime(2026, 7, 9, 21, 0, tzinfo=timezone.utc)  # and UTC+3 in summer
    assert core.local_midnight_utc("", TZ) is None
