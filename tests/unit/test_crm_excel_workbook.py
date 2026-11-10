"""The CRM workbook generator: layout, safety, signing, identity rules and dashboard maths.

These tests need no database: the exporter is a pure function of ``ExportData``.
The dashboard formulas are checked by recalculating the real file in LibreOffice
when it is installed (skipped otherwise).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.crm.excel import identity, schema as sc, signing
from app.crm.excel.baseline import changed_fields, lead_baseline
from app.crm.excel.data import DuplicateRow, ExportData, InteractionRow, LeadRow, TaskRow, ViewingRow
from app.crm.excel.exporter import build_workbook
from app.crm.excel.meta import WorkbookRejected, read_meta, verify_meta
from app.crm.excel.text import clean, code, excerpt, fingerprint, parse_code, requirements_summary

KEY = signing.derive_key(None, "unit-test-admin-key-0123456789")
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def lead(pk: int, **overrides) -> LeadRow:
    values = dict(
        pk=pk, uuid=f"00000000-0000-0000-0000-{pk:012d}", customer_pk=pk, name=f"Customer {pk}", phone=f"+2010000000{pk:02d}",
        channel="website", created_at=NOW - timedelta(days=pk), score=50, band="warm", status="new",
    )
    values.update(overrides)
    return LeadRow(**values)


def task(pk: int, lead_pk: int = 1, **overrides) -> TaskRow:
    values = dict(pk=pk, uuid=f"t-{pk}", lead_pk=lead_pk, customer=f"Customer {lead_pk}", task_type="call_customer",
                  description="Call back", priority="high", status="open", source="system", created_at=NOW)
    values.update(overrides)
    return TaskRow(**values)


def export(data: ExportData, agents=("Mona", "Ahmed"), key: bytes = KEY) -> bytes:
    return build_workbook(data, agents=list(agents), timezone="Africa/Cairo", signing_key=key)


def opened(blob: bytes):
    return load_workbook(io.BytesIO(blob))


def sample() -> ExportData:
    leads = [
        lead(1, status="new", channel="website", location="New Cairo", transaction_type="sale", qualified=True, band="hot", score=85),
        lead(2, status="qualified", channel="whatsapp", location="New Cairo", transaction_type="rent", qualified=True),
        lead(3, status="contacted", channel="whatsapp", location="Sheikh Zayed", band="cold", assigned_agent="Mona", next_followup_at=NOW - timedelta(days=2)),
        lead(4, status="won", channel="website_form", location="Sheikh Zayed", transaction_type="buy", qualified=True),
        lead(5, status="lost", location=""),
        lead(6, status="negotiation", location="6th October", assigned_agent="Ghost", next_followup_at=NOW, open_handoff=True),
    ]
    tasks = [
        task(1, 1, due_at=NOW, assigned_agent="Mona"),
        task(2, 3, task_type="confirm_viewing", status="in_progress", due_at=NOW - timedelta(days=3), priority="urgent"),
        task(3, 2, task_type="other", due_at=NOW + timedelta(days=5), priority="low", source="staff"),
        task(4, 2, task_type="other", status="done", due_at=NOW - timedelta(days=1), completed_at=NOW),
    ]
    viewings = [
        ViewingRow(1, 2, "Customer 2", "+201000000002", "501189810", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1), "confirmed"),
        ViewingRow(2, 3, "Customer 3", "", "501189811", NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1), "pending_confirmation"),
        ViewingRow(3, 4, "Customer 4", "", "501189812", NOW - timedelta(days=2), NOW - timedelta(days=2, hours=-1), "completed"),
    ]
    return ExportData(generated_at=NOW, leads=leads, tasks=tasks, viewings=viewings)


# ---------------------------------------------------------------------------
# Schema rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        ("new", "contacted", True), ("qualifying", "qualified", True), ("hot", "negotiation", True), ("viewing_booked", "negotiation", True),
        ("new", "negotiation", False), ("handoff_requested", "negotiation", False),
        ("negotiation", "won", True), ("contacted", "won", True), ("new", "won", False), ("hot", "won", False),
        ("negotiation", "lost", True), ("new", "lost", True),
        ("lost", "contacted", True), ("lost", "qualified", False), ("won", "contacted", False), ("won", "lost", False), ("closed", "contacted", False),
        ("new", "viewing_booked", False), ("new", "hot", False), ("contacted", "new", False),
        ("won", "won", True), ("hot", "hot", True),
    ],
)
def test_manual_status_changes_follow_the_lifecycle(current, target, allowed):
    assert sc.can_set_status(current, target) is allowed


def test_every_status_the_assistant_writes_has_a_label():
    from app.agents.tools.crm import _STATUS_RANK

    assert set(_STATUS_RANK) <= set(sc.LEAD_STATUS)


def test_choices_parse_from_labels_or_keys_and_reject_unknown_values():
    assert sc.parse_choice(sc.LEAD_STATUS, "Contacted | تم التواصل") == "contacted"
    assert sc.parse_choice(sc.LEAD_STATUS, "  contacted ") == "contacted"
    assert sc.parse_choice(sc.LEAD_STATUS, "Nonsense") is None
    assert sc.parse_choice(sc.DUPLICATE_DECISION, "") == ""
    assert sc.parse_choice(sc.TASK_TYPE, None) is None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def test_excerpt_is_short_single_line_and_cuts_at_a_word():
    long = "I am looking for a villa in Sheikh Zayed " * 30
    cut = excerpt(long, 300)
    assert len(cut) <= 300 and cut.endswith("…") and "\n" not in cut
    assert excerpt("  a\n\n b\t c  ") == "a b c"
    assert excerpt(None) == "" and excerpt("short") == "short"
    arabic = excerpt("عايز فيلا في الشيخ زايد " * 40, 300)
    assert len(arabic) <= 300


def test_clean_strips_control_characters_and_truncates_visibly():
    assert clean("a\x00b\x07c\r\nd") == "abc\nd"
    assert clean("x" * 50, limit=30).endswith("[truncated]") and len(clean("x" * 50, limit=30)) == 30


def test_record_codes_round_trip():
    assert code("L", 123) == "L-000123"
    assert parse_code("L", " l-000123 ") == 123 and parse_code("L", "T-000123") is None and parse_code("L", "L-12x") is None


def test_fingerprint_ignores_edge_whitespace_and_hashes_long_text():
    assert fingerprint("  hello ") == "hello"
    long = "n" * 500
    assert fingerprint(long).startswith("sha256:") and fingerprint(long) == fingerprint(f" {long}\n") and fingerprint(long) != fingerprint(long + "!")


def test_requirements_summary_skips_what_has_its_own_column():
    summary = requirements_summary({"district": "New Cairo", "property_type": "villa", "finishing": "full", "amenities": ["pool", "gym"], "min_price": 1})
    assert summary == "finishing: full; amenities: pool, gym"
    assert requirements_summary(None) == "" and requirements_summary({"bedrooms": 3}) == ""


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_signature_detects_any_change():
    header = signing.make_header("abc", "2026-11-06T10:00:00+00:00", "Africa/Cairo")
    rows = [("lead", "u1", '{"status":"new"}'), ("task", "t1", '{"status":"open"}')]
    signature = signing.sign(KEY, header, rows)
    assert signing.verify(KEY, header, list(reversed(rows)), signature)  # row order is irrelevant
    assert not signing.verify(KEY, header, [rows[0], ("task", "t1", '{"status":"done"}')], signature)
    assert not signing.verify(KEY, {**header, "export_id": "zzz"}, rows, signature)
    assert not signing.verify(signing.derive_key(None, "another-admin-key-0123456789"), header, rows, signature)


def test_signing_key_comes_from_the_explicit_key_before_the_admin_key():
    assert signing.derive_key("explicit", "admin") != signing.derive_key(None, "admin")
    assert signing.derive_key("explicit", "admin") == signing.derive_key("explicit", "other-admin")
    with pytest.raises(signing.SigningUnavailable):
        signing.derive_key(None, None)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def facts(pk, phone="+201000000001", email=None, channel="website", verified=False, method=None):
    return identity.CustomerFacts(pk, f"C{pk}", phone, email, channel, verified, method)


def test_a_website_phone_is_never_enough_to_link_customers():
    web = facts(1, channel="website", verified=True, method="readback")
    wa = facts(2, channel="whatsapp", verified=True, method="whatsapp_sender")
    (candidate,) = identity.find_candidates([web, wa])
    assert candidate.evidence == "same_phone" and not candidate.auto_linked
    assert identity.linked_pairs([candidate], {}) == set()
    assert identity.person_keys(identity.linked_pairs([candidate], {})) == {}


def test_a_number_verified_on_both_sides_links_automatically():
    a = facts(1, verified=True, method="otp")
    b = facts(2, channel="whatsapp", verified=True, method="whatsapp_sender")
    (candidate,) = identity.find_candidates([a, b])
    assert candidate.auto_linked and candidate.evidence == "same_phone_verified_both"
    assert identity.person_keys(identity.linked_pairs([candidate], {})) == {1: "P-000001", 2: "P-000001"}


def test_staff_decisions_override_the_evidence():
    a, b = facts(1, verified=True, method="otp"), facts(2, verified=True, method="whatsapp_sender")
    (strong,) = identity.find_candidates([a, b])
    assert identity.linked_pairs([strong], {(1, 2): "rejected"}) == set()

    (weak,) = identity.find_candidates([facts(3), facts(4)])
    assert identity.linked_pairs([weak], {(3, 4): "confirmed"}) == {(3, 4)}
    assert identity.pending_candidates([weak], {}) == [weak] and identity.pending_candidates([weak], {(3, 4): "rejected"}) == []


def test_linked_groups_share_one_person_key_and_a_shared_email_is_a_weak_signal():
    keys = identity.person_keys({(1, 2), (2, 5), (7, 9)})
    assert keys[1] == keys[2] == keys[5] == "P-000001" and keys[7] == keys[9] == "P-000007"
    (by_email,) = identity.find_candidates([facts(1, phone=None, email="A@x.com"), facts(2, phone=None, email="a@X.com", channel="whatsapp")])
    assert by_email.evidence == "same_email" and not by_email.auto_linked


def test_a_number_shared_by_many_records_is_treated_as_noise():
    assert identity.find_candidates([facts(i) for i in range(1, identity.MAX_GROUP + 2)]) == []


# ---------------------------------------------------------------------------
# Workbook layout and safety
# ---------------------------------------------------------------------------


def test_sheets_headers_and_hidden_parts():
    book = opened(export(sample()))
    assert book.sheetnames == ["Guide", "Dashboard", "Leads", "Interactions", "Tasks", "Viewings", "Duplicates", "Lists", "_meta"]
    assert book["_meta"].sheet_state == "veryHidden"
    assert book.active.title == "Dashboard" and [s.title for s in book if s.sheet_view.tabSelected] == ["Dashboard"]
    for name, columns in sc.COLUMNS_BY_SHEET.items():
        assert [cell.value for cell in book[name][1]] == [column.header for column in columns]
    leads = book["Leads"]
    assert leads.column_dimensions["B"].hidden and leads.freeze_panes == "E2"
    assert leads["E1"].fill.start_color.rgb.endswith("FFF2CC") and leads["C1"].fill.start_color.rgb.endswith("DDEBF7")  # editable vs read-only


def test_values_are_shown_in_cairo_time_with_bilingual_labels():
    late = datetime(2026, 11, 6, 22, 30, tzinfo=timezone.utc)  # already the 7th in Cairo (UTC+2)
    blob = export(ExportData(generated_at=late, leads=[lead(1, status="contacted", next_followup_at=late, qualified=True, band="hot")]))
    leads = opened(blob)["Leads"]
    assert leads.cell(row=2, column=sc.column_index("Leads", "next_followup")).value.date().isoformat() == "2026-11-07"
    assert leads.cell(row=2, column=sc.column_index("Leads", "status")).value == "Contacted | تم التواصل"
    assert leads.cell(row=2, column=sc.column_index("Leads", "qualified")).value == sc.YES
    assert leads.cell(row=2, column=sc.column_index("Leads", "band")).value == "Hot | ساخن"


@pytest.mark.parametrize("hostile", ['=HYPERLINK("http://evil","x")', "+1+1", "-2+3", "@SUM(1+1)", "=cmd|' /C calc'!A0", "\t=1+1"])
def test_customer_text_can_never_become_a_formula(hostile):
    data = ExportData(
        generated_at=NOW,
        leads=[lead(1, name=hostile, notes=hostile)],
        tasks=[task(1, description=hostile)],
        interactions=[InteractionRow(1, 1, hostile, NOW, "website", "text", hostile, hostile)],
    )
    book = opened(export(data))
    for sheet, key in (("Leads", "name"), ("Leads", "notes"), ("Tasks", "description"), ("Interactions", "customer_message"), ("Interactions", "assistant_response")):
        cell = book[sheet].cell(row=2, column=sc.column_index(sheet, key))
        assert cell.data_type == "s" and cell.value == hostile.replace("\r", "")


def test_an_empty_export_is_still_a_valid_workbook_with_room_to_type():
    book = opened(export(ExportData(generated_at=NOW)))
    for name in ("Leads", "Tasks", "Interactions", "Viewings", "Duplicates"):
        assert book[name].tables[sc.TABLE_NAMES[name]].ref.endswith("2")  # header plus one blank row
    assert read_meta(book).rows == []


def test_new_task_rows_already_have_dropdowns():
    book = opened(export(sample()))
    spans = {str(v.sqref): v.formula1 for v in book["Tasks"].data_validations.dataValidation}
    last_validated = max(int(span.split(":")[1][len("F"):]) for span in spans if span.startswith("F"))
    assert last_validated >= len(sample().tasks) + 400
    assert {"List_TaskType", "List_Agents", "List_Priority", "List_TaskStatus"} <= set(spans.values())


def test_agent_dropdown_is_strict_only_when_a_roster_is_configured():
    def strict(agents):
        book = opened(export(sample(), agents=agents))
        return next(v for v in book["Leads"].data_validations.dataValidation if v.formula1 == "List_Agents").showErrorMessage

    assert strict(["Mona"]) is True and strict([]) is False


def test_people_already_assigned_stay_in_the_dropdown_list():
    values = [cell.value for cell in opened(export(sample(), agents=["Mona"]))["Lists"]["B"][1:] if cell.value]
    assert values == ["Mona", "Ghost"]  # "Ghost" is assigned in the data but not in the roster


def test_export_of_thousands_of_rows_stays_fast_and_small():
    import time

    many = ExportData(generated_at=NOW, leads=[lead(i, notes="note " * 20, location=f"Area {i % 40}") for i in range(1, 4001)],
                      tasks=[task(i, (i % 4000) + 1) for i in range(1, 2001)])
    started = time.perf_counter()
    blob = export(many)
    assert time.perf_counter() - started < 25 and len(blob) < 5_000_000


# ---------------------------------------------------------------------------
# Baseline and signature inside the file
# ---------------------------------------------------------------------------


def test_the_baseline_matches_what_the_importer_will_compare_against():
    from zoneinfo import ZoneInfo

    data = sample()
    meta = read_meta(opened(export(data)))
    verify_meta(meta, KEY)
    baseline = meta.baseline("lead")
    assert set(baseline) == {l.uuid for l in data.leads} and set(meta.baseline("task")) == {t.uuid for t in data.tasks}
    third = data.leads[2]
    expected = lead_baseline(status=third.status, assigned_agent=third.assigned_agent, next_followup_at=third.next_followup_at,
                             last_contact=third.last_contact_at, notes=third.notes, tz=ZoneInfo("Africa/Cairo"))
    assert baseline[third.uuid] == expected and expected["status"] == "contacted" and expected["agent"] == "Mona"
    assert changed_fields({"status": "won", "agent": "Mona"}, expected) == ["status"]


def test_a_tampered_or_foreign_workbook_is_rejected():
    blob = export(sample())
    book = opened(blob)
    book["_meta"].cell(row=9, column=3).value = '{"status":"won"}'  # edit a baseline row
    buffer = io.BytesIO()
    book.save(buffer)
    with pytest.raises(WorkbookRejected, match="signature"):
        verify_meta(read_meta(opened(buffer.getvalue())), KEY)
    with pytest.raises(WorkbookRejected, match="signature"):  # another deployment's key
        verify_meta(read_meta(opened(blob)), signing.derive_key(None, "a-different-deployment-key-0123"))

    plain = io.BytesIO()
    from openpyxl import Workbook

    Workbook().save(plain)
    with pytest.raises(WorkbookRejected, match="_meta"):
        read_meta(opened(plain.getvalue()))


def test_notes_too_long_for_a_cell_are_flagged_as_not_editable():
    from app.crm.excel.text import CELL_LIMIT

    meta = read_meta(opened(export(ExportData(generated_at=NOW, leads=[lead(1, notes="n" * (CELL_LIMIT + 500))]))))
    assert meta.baseline("lead")[lead(1).uuid]["notes_cut"] is True


# ---------------------------------------------------------------------------
# Dashboard formulas, recalculated by a real spreadsheet engine
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_dashboard_numbers_match_the_data_after_recalculation():
    blob = export(sample())
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder, "crm.xlsx")
        source.write_bytes(blob)
        # TODAY()/NOW() follow the machine's zone; the export is in Cairo time.
        subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", str(Path(folder, "out")), str(source)],
                       check=True, capture_output=True, timeout=180, env={**os.environ, "TZ": "Africa/Cairo"})
        sheet = load_workbook(Path(folder, "out", "crm.xlsx"), data_only=True)["Dashboard"]
    values = {str(row[0]): row[1] for row in sheet.iter_rows(max_col=2, values_only=True) if row[0] is not None}
    pick = lambda prefix: next(v for k, v in values.items() if k.startswith(prefix))  # noqa: E731

    assert pick("Total leads") == 6 and pick("New leads") == 1
    assert pick("Qualified leads") == 3 and pick("Unqualified leads") == 3
    assert (pick("Hot leads"), pick("Warm leads"), pick("Cold leads")) == (1, 4, 1)  # bands come from LeadRow.band
    assert pick("Follow-ups due today") == 1 and pick("Overdue follow-ups") == 1  # the done task and the future one do not count
    assert pick("Upcoming viewings") == 2
    assert round(pick("Conversion rate"), 4) == round(1 / 6, 4) and pick("Win rate") == 0.5
    assert pick("Buy |") == 2 and pick("Rent |") == 1 and pick("Not specified |") == 3
    assert pick("New Cairo") == 2 and pick("Other or not specified") == 1
    assert pick("Won |") == 1 and pick("Lost |") == 1 and pick("Negotiation |") == 1
