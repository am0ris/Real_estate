"""The ten scenarios the CRM has to get right, driven end to end.

Each one starts from what a customer or a salesperson actually does (real conversation
turns through the assistant, real workbook downloads, real uploads) and checks what the
sales team sees in the Excel file and what is stored in PostgreSQL.

 1 new website customer              6 several customers at once (interleaved here;
 2 new WhatsApp customer                 truly concurrent in test_crm_scenarios_postgres.py)
 3 same customer, same channel       7 the assistant and a salesperson update one lead
 4 same person, other channel        8 the workbook side is down, locked or broken
 5 requirements arrive gradually     9 the customer asks for a human
                                    10 a salesperson changes status / follow-up date
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from app.crm.excel import schema as sc
from app.crm.excel.service import crm_workbook_service
from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_api_channels import client  # noqa: F401  (fixture)
from tests.integration.test_booking_flows import say
from tests.integration.test_crm_import import Editor, lead_of, run

ADMIN = {"X-Admin-Key": "test-admin-key-0123456789abcdef"}
RAW = {**ADMIN, "Content-Type": "application/octet-stream"}
TODAY = datetime.now(ZoneInfo("Africa/Cairo")).date()
HOT_LEAD_TEXT = "villa in Sheikh Zayed, my budget is 9 million, for investment, within 3 months"  # scores as a hot, qualified lead


def blob():
    return crm_workbook_service.export().content


def rows(content, sheet):
    """What the sales team sees on one sheet, as {column key: value} per row."""
    columns = sc.COLUMNS_BY_SHEET[sheet]
    sheet_rows = load_workbook(io.BytesIO(content))[sheet].iter_rows(min_row=2, values_only=True)
    return [dict(zip((c.key for c in columns), values)) for values in sheet_rows if any(v not in (None, "") for v in values)]


def status_label(key):
    return sc.label(sc.LEAD_STATUS, key)


def new_web():
    return f"web:{uuid4().hex}"


def customers():
    with session_scope() as session:
        return session.query(db.Customer).count()


# ---------------------------------------------------------------------------
# 1 - a new customer on the website
# ---------------------------------------------------------------------------


def test_1_a_new_website_customer_becomes_a_scored_lead_with_their_conversation(service, stub_rag):
    say(service, new_web(), HOT_LEAD_TEXT)
    content = blob()

    (lead,) = rows(content, sc.LEADS)
    assert lead["channel"] == "website" and lead["location"] == "Sheikh Zayed" and lead["budget_max"] == 9_000_000
    assert lead["status"] == status_label("qualifying") and lead["band"] == sc.BAND["hot"] and lead["qualified"] == sc.YES
    assert lead["phone"] in (None, "") and lead["assigned_agent"] in (None, "")

    (interaction,) = rows(content, sc.INTERACTIONS)
    assert (interaction["channel"], interaction["modality"], interaction["lead_ref"]) == ("website", "text", lead["lead_ref"])
    assert "Sheikh Zayed" in interaction["customer_message"] and len(interaction["customer_message"]) <= 300

    (task,) = rows(content, sc.TASKS)  # a hot lead nobody has contacted yet is flagged for sales
    assert task["task_type"] == sc.TASK_TYPE["call_customer"] and task["priority"] == sc.PRIORITY["high"] and task["lead_ref"] == lead["lead_ref"]


# ---------------------------------------------------------------------------
# 2 - a new customer on WhatsApp
# ---------------------------------------------------------------------------


def test_2_a_new_whatsapp_customer_arrives_with_a_verified_number(service, stub_rag):
    say(service, "whatsapp:201012345678", "apartment in New Cairo", channel="whatsapp")
    (lead,) = rows(blob(), sc.LEADS)
    with session_scope() as session:
        customer = session.query(db.Customer).one()
        assert customer.phone_verified is True and customer.phone_verification_method == "whatsapp_sender"
    assert lead["channel"] == "whatsapp" and lead["phone"] == customer.phone and lead["phone"].endswith("1012345678")
    assert lead["phone_verified"] == sc.YES and lead["location"] == "New Cairo"


# ---------------------------------------------------------------------------
# 3 - the same customer comes back on the same channel
# ---------------------------------------------------------------------------


def test_3_a_returning_customer_updates_their_lead_instead_of_creating_another(service, stub_rag):
    thread = new_web()
    say(service, thread, "villa in Sheikh Zayed")
    first = rows(blob(), sc.LEADS)
    say(service, thread, "my budget is 9 million")  # a later session
    content = blob()

    (lead,) = rows(content, sc.LEADS)
    assert lead["lead_ref"] == first[0]["lead_ref"] and customers() == 1
    assert lead["location"] == "Sheikh Zayed" and lead["budget_max"] == 9_000_000  # kept the earlier detail and added the new one
    assert [i["lead_ref"] for i in rows(content, sc.INTERACTIONS)] == [lead["lead_ref"]] * 2  # nothing overwritten: both turns are logged


# ---------------------------------------------------------------------------
# 4 - the same person on a different channel
# ---------------------------------------------------------------------------


def web_customer_with_phone(service):
    thread = new_web()
    say(service, thread, "apartment in New Cairo, budget 30 million, for investment")
    say(service, thread, "I want to talk to a sales agent")
    say(service, thread, "01012345678")  # typed in chat: never treated as verified
    return thread


def test_4_the_same_phone_on_another_channel_is_flagged_for_review_never_merged(service, stub_rag):
    web = web_customer_with_phone(service)
    say(service, "whatsapp:201012345678", "villa in Sheikh Zayed", channel="whatsapp")
    content = blob()

    leads = rows(content, sc.LEADS)
    assert len(leads) == 2 and customers() == 2  # two records, as designed: no silent merge
    assert {l["possible_duplicate"] for l in leads} == {sc.YES} and {l["person"] for l in leads} == {None}
    (pair,) = rows(content, sc.DUPLICATES)
    assert pair["evidence"] == sc.EVIDENCE["same_phone"] and pair["decision"] == sc.DUPLICATE_DECISION[""]
    assert {pair["a_verified"], pair["b_verified"]} == {sc.YES, sc.NO}  # only the WhatsApp sender is verified

    # What one channel told the assistant never leaks into the other conversation.
    assert preferred_location(web) == "New Cairo" and preferred_location("whatsapp:201012345678") == "Sheikh Zayed"


def test_4_staff_can_confirm_the_pair_and_the_records_stay_separate(service, stub_rag):
    web_customer_with_phone(service)
    say(service, "whatsapp:201012345678", "villa in Sheikh Zayed", channel="whatsapp")
    before = sorted(session_values(db.Customer.external_id, db.Customer.phone))

    run(Editor(blob()).duplicate(decision=sc.DUPLICATE_DECISION["confirmed"]).bytes(), dry_run=False)
    leads = rows(blob(), sc.LEADS)
    assert len({l["person"] for l in leads}) == 1 and leads[0]["person"].startswith("P-")  # grouped for the sales team
    assert sorted(session_values(db.Customer.external_id, db.Customer.phone)) == before  # customer rows untouched


def preferred_location(thread):
    with session_scope() as session:
        customer = session.query(db.Customer).filter_by(external_id=thread).one()
        return session.query(db.Lead).filter_by(customer_id=customer.id).one().preferred_location


def session_values(*columns):
    with session_scope() as session:
        return [tuple(row) for row in session.query(*columns).all()]


# ---------------------------------------------------------------------------
# 5 - requirements arrive over several messages
# ---------------------------------------------------------------------------


def test_5_a_lead_grows_message_by_message_without_ever_being_duplicated(service, stub_rag):
    thread = new_web()
    seen = []
    for text in ("apartment in New Cairo", "my budget is 5 million", "for investment, within 3 months"):
        say(service, thread, text)
        (lead,) = rows(blob(), sc.LEADS)  # always exactly one
        seen.append(lead)

    assert seen[0]["location"] == "New Cairo" and seen[0]["budget_max"] is None
    assert seen[1]["location"] == "New Cairo" and seen[1]["budget_max"] == 5_000_000
    assert seen[2]["location"] == "New Cairo" and seen[2]["budget_max"] == 5_000_000 and seen[2]["qualified"] == sc.YES
    assert seen[0]["score"] < seen[1]["score"] < seen[2]["score"]
    assert {l["lead_ref"] for l in seen} == {seen[0]["lead_ref"]} and customers() == 1


# ---------------------------------------------------------------------------
# 6 - many customers at once (interleaved turns; real threads are in the Postgres file)
# ---------------------------------------------------------------------------


def test_6_interleaved_customers_never_bleed_into_each_other(service, stub_rag):
    districts = ["New Cairo", "Sheikh Zayed"]
    threads = [new_web() for _ in range(8)]
    for round_number in range(2):  # round-robin, the way simultaneous visitors interleave
        for index, thread in enumerate(threads):
            if round_number == 0:
                say(service, thread, f"apartment in {districts[index % 2]}")
            else:
                say(service, thread, f"my budget is {index + 2} million")
    content = blob()

    leads = rows(content, sc.LEADS)
    assert len(leads) == 8 and len({l["lead_uuid"] for l in leads}) == 8 and customers() == 8
    by_budget = {l["budget_max"]: l["location"] for l in leads}
    assert by_budget == {(i + 2) * 1_000_000: districts[i % 2] for i in range(8)}  # each customer kept their own details
    assert len(rows(content, sc.INTERACTIONS)) == 16


# ---------------------------------------------------------------------------
# 7 - the assistant and a salesperson both update the same lead
# ---------------------------------------------------------------------------


def test_7_the_assistants_update_and_the_salespersons_edit_both_survive(service, stub_rag, settings_env):
    settings_env(CRM_AGENTS="Mona, Ahmed")
    thread = new_web()
    say(service, thread, "apartment in New Cairo")
    file = blob()  # downloaded; the salesperson now works in Excel...

    say(service, thread, "my budget is 5 million")  # ...while the customer keeps chatting
    (lead_row,) = rows(file, sc.LEADS)
    follow_up = datetime(TODAY.year, TODAY.month, TODAY.day) + timedelta(days=2)
    result = run(Editor(file).lead(int(lead_row["lead_ref"][2:]), assigned_agent="Mona", next_followup=follow_up).bytes(), dry_run=False)

    assert result.report["summary"]["conflicts"] == 0 and result.report["summary"]["errors"] == 0
    (after,) = rows(blob(), sc.LEADS)
    assert after["assigned_agent"] == "Mona" and after["next_followup"].date() == TODAY + timedelta(days=2)  # the salesperson's edit
    assert after["budget_max"] == 5_000_000 and after["location"] == "New Cairo"  # the assistant's update


def test_7_when_both_touch_the_same_field_nothing_is_overwritten_and_the_conflict_is_reported(service, stub_rag):
    thread = new_web()
    say(service, thread, "apartment in New Cairo")
    file = blob()
    (lead_row,) = rows(file, sc.LEADS)
    pk = int(lead_row["lead_ref"][2:])
    with session_scope() as session:
        session.get(db.Lead, pk).notes = "[2026-11-10 10:00] customer asked for a viewing"  # the assistant's note, after the download

    result = run(Editor(file).lead(pk, notes="my note").bytes(), dry_run=False)
    assert result.report["summary"]["conflicts"] == 1
    assert lead_of(pk)["notes"] == "[2026-11-10 10:00] customer asked for a viewing"


# ---------------------------------------------------------------------------
# 8 - the workbook side is down, locked or broken
# ---------------------------------------------------------------------------


def test_8_a_broken_export_never_stops_the_assistant_and_loses_no_lead(client, service, stub_rag, monkeypatch):  # noqa: F811
    import app.crm.excel.service as workbook_service

    thread = new_web()
    say(service, thread, "apartment in New Cairo")
    working_reader = workbook_service.load_export_data
    monkeypatch.setattr(workbook_service, "load_export_data", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database read failed")))
    with pytest.raises(RuntimeError):
        client.get("/admin/crm/export.xlsx", headers=ADMIN)
    say(service, thread, "my budget is 5 million")  # the conversation carries on regardless
    assert client.post("/chat", json={"query": "apartments in New Cairo", "session_id": None}).status_code == 200

    monkeypatch.setattr(workbook_service, "load_export_data", working_reader)  # the problem is fixed: nothing was lost in the meantime
    (lead,) = rows(blob(), sc.LEADS)
    assert lead["location"] == "New Cairo" and lead["budget_max"] == 5_000_000


def test_8_a_corrupt_or_locked_upload_is_refused_and_changes_nothing(client):  # noqa: F811
    from tests.integration.test_crm_excel_queries import make_lead

    _, lead = make_lead()
    before = lead_of(lead)
    for broken in (b"~$crm.xlsx lock file", b"PK\x03\x04 half a file", b""):  # an Excel lock file, a truncated save, nothing
        assert client.post("/admin/crm/import", params={"dry_run": "false"}, content=broken, headers=RAW).status_code == 400
    assert lead_of(lead) == before


def test_8_missing_workbook_packages_disable_only_the_crm_routes(client, service, stub_rag, monkeypatch):  # noqa: F811
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # importing it now raises ImportError
    for name in [m for m in sys.modules if m.startswith(("app.crm.excel.exporter", "app.crm.excel.service", "app.crm.excel.importer"))]:
        monkeypatch.delitem(sys.modules, name)  # force the lazy import to run again

    export = client.get("/admin/crm/export.xlsx", headers=ADMIN)
    upload = client.post("/admin/crm/import", content=b"anything", headers=RAW)
    assert export.status_code == 503 and "openpyxl" in export.json()["detail"] and upload.status_code == 503
    say(service, new_web(), "apartment in New Cairo")  # the assistant is unaffected
    assert client.post("/chat", json={"query": "apartments in New Cairo", "session_id": None}).status_code == 200
    assert client.get("/admin/leads", headers=ADMIN).status_code == 200  # and so is the rest of the admin API


# ---------------------------------------------------------------------------
# 9 - the customer asks for a human
# ---------------------------------------------------------------------------


def test_9_a_request_for_a_human_becomes_one_urgent_task_that_closes_when_it_is_handled(client, service, stub_rag):  # noqa: F811
    thread = web_customer_with_phone(service)

    content = blob()
    (lead,) = rows(content, sc.LEADS)
    assert lead["status"] == status_label("handoff_requested") and lead["open_handoff"] == sc.YES and lead["phone"].endswith("1012345678")
    (task,) = rows(content, sc.TASKS)
    assert task["task_type"] == sc.TASK_TYPE["call_customer"] and task["priority"] == sc.PRIORITY["high"] and task["status"] == sc.TASK_STATUS["open"]
    assert "sales representative" in task["description"] and "موظف مبيعات" in task["description"]
    assert len(rows(blob(), sc.TASKS)) == 1  # downloading again does not create another

    ticket = client.get("/admin/handoffs", headers=ADMIN).json()[0]["id"]
    client.post(f"/admin/handoffs/{ticket}/claim", headers=ADMIN, json={"by": "Omar"})
    client.post(f"/admin/handoffs/{ticket}/resolve", headers=ADMIN, json={"by": "Omar"})

    content = blob()
    (lead,) = rows(content, sc.LEADS)
    (task,) = rows(content, sc.TASKS)
    assert task["status"] == sc.TASK_STATUS["done"] and task["completed"] is not None
    assert lead["open_handoff"] == sc.NO and lead["status"] == status_label("contacted") and lead["last_contact"].date() == TODAY


# ---------------------------------------------------------------------------
# 10 - a salesperson changes the status or the follow-up date
# ---------------------------------------------------------------------------


def test_10_status_changes_made_in_excel_stick_when_the_customer_writes_again(service, stub_rag, settings_env):
    settings_env(CRM_AGENTS="Mona, Ahmed")
    thread = new_web()
    say(service, thread, HOT_LEAD_TEXT)

    def edit(**cells):
        (current,) = [l for l in rows(blob(), sc.LEADS) if l["status"] != status_label("won")][:1]
        return run(Editor(blob()).lead(int(current["lead_ref"][2:]), **cells).bytes(), dry_run=False)

    result = edit(status=sc.LEAD_STATUS["contacted"], assigned_agent="Mona", next_followup=datetime(TODAY.year, TODAY.month, TODAY.day) + timedelta(days=3))
    assert result.report["summary"]["errors"] == 0
    (lead,) = rows(blob(), sc.LEADS)
    assert lead["status"] == status_label("contacted") and lead["assigned_agent"] == "Mona" and lead["next_followup"].date() == TODAY + timedelta(days=3)
    assert lead["last_contact"].date() == TODAY  # was empty, so today
    assert [t["task_type"] for t in rows(blob(), sc.TASKS) if t["status"] == sc.TASK_STATUS["open"]] == [sc.TASK_TYPE["follow_up"]]  # the hot-lead task closed; the follow-up remains

    edit(status=sc.LEAD_STATUS["negotiation"])
    say(service, thread, "what about a villa in New Cairo")  # the assistant works on the lead again
    say(service, thread, "I can pay cash and decide this week")
    (lead,) = rows(blob(), sc.LEADS)
    assert lead["status"] == status_label("negotiation") and lead["assigned_agent"] == "Mona"  # the assistant never pulls it back


def test_10_a_won_lead_stays_won_and_a_returning_customer_starts_a_new_lead(service, stub_rag):
    thread = new_web()
    say(service, thread, HOT_LEAD_TEXT)
    for status in ("contacted", "negotiation", "won"):
        (current,) = [l for l in rows(blob(), sc.LEADS) if l["status"] != status_label("won")]
        run(Editor(blob()).lead(int(current["lead_ref"][2:]), status=sc.LEAD_STATUS[status]).bytes(), dry_run=False)

    say(service, thread, "I'm looking for another apartment in New Cairo")
    statuses = sorted(l["status"] for l in rows(blob(), sc.LEADS))
    assert len(statuses) == 2 and status_label("won") in statuses and customers() == 1
