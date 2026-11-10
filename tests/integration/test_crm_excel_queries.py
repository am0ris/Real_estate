"""The database projection behind the CRM workbook (read-only) and a full export from real rows."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from openpyxl import load_workbook

from app.crm.excel import schema as sc, signing
from app.crm.excel.exporter import build_workbook
from app.crm.excel.meta import read_meta, verify_meta
from app.crm.excel.queries import load_export_data
from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_booking_flows import say

NOW = datetime.now(timezone.utc).replace(microsecond=0)
KEY = signing.derive_key(None, "integration-admin-key-0123456789")


def load(**kwargs):
    with session_scope() as session:
        return load_export_data(session, now=kwargs.pop("now", NOW), **kwargs)


def make_lead(external_id=None, *, status="new", phone=None, verified=False, method=None, name=None, email=None, source="website", score=0, **lead_fields):
    """A customer with one lead, inserted directly. Returns (customer pk, lead pk)."""
    external_id = external_id or f"web:{uuid4().hex}"
    with session_scope() as session:
        customer = db.Customer(
            external_id=external_id, name=name, phone=phone, email=email, source=source,
            phone_verified=verified, phone_verification_method=method,
        )
        session.add(customer)
        session.flush()
        lead = db.Lead(customer_id=customer.id, status=status, source=source, score=score, **lead_fields)
        session.add(lead)
        session.flush()
        return customer.id, lead.id


def row_for(data, lead_pk):
    return next(row for row in data.leads if row.pk == lead_pk)


# ---------------------------------------------------------------------------
# Leads from real conversations
# ---------------------------------------------------------------------------


def test_a_real_conversation_shows_up_as_a_scored_lead_with_its_interaction(service, thread_id, stub_rag):
    say(service, thread_id, "villa in Sheikh Zayed, my budget is 9 million, for investment, within 3 months")
    data = load()
    (lead,) = data.leads
    assert lead.status == "qualifying" and lead.channel == "website" and lead.location == "Sheikh Zayed"
    assert lead.budget_max == 9_000_000 and lead.band == "hot" and lead.score >= 70 and lead.qualified is True
    assert lead.last_message_at is not None and lead.assigned_agent == "" and lead.next_followup_at is None

    (interaction,) = data.interactions
    assert interaction.lead_pk == lead.pk and interaction.channel == "website" and interaction.modality == "text"
    assert "Sheikh Zayed" in interaction.customer_message and len(interaction.customer_message) <= 300 and len(interaction.assistant_response) <= 300


def test_long_messages_are_cut_to_an_excerpt_in_the_workbook_only(service, thread_id, stub_rag):
    say(service, thread_id, "villa in Sheikh Zayed " + "with a big garden and a pool " * 40)
    (interaction,) = load().interactions
    assert len(interaction.customer_message) <= 300 and interaction.customer_message.endswith("…")
    with session_scope() as session:  # the stored conversation is untouched
        assert len(session.query(db.Interaction).one().content) > 1000


def test_voice_and_whatsapp_turns_keep_their_channel_and_type(service, stub_rag):
    thread = "whatsapp:201012345678"
    say(service, thread, "apartment in New Cairo", channel="whatsapp", modality="voice")
    (interaction,) = load().interactions
    assert (interaction.channel, interaction.modality) == ("whatsapp", "voice")


def test_closed_leads_can_be_left_out_and_always_sort_last():
    _, open_lead = make_lead(status="qualified", score=10)
    _, won = make_lead(status="won", score=99)
    _, hot = make_lead(status="hot", score=90)
    everything = load()
    assert [row.pk for row in everything.leads] == [hot, open_lead, won]
    assert [row.pk for row in load(include_closed=False).leads] == [hot, open_lead]


def test_the_export_caps_report_what_was_left_out():
    for _ in range(3):
        make_lead()
    data = load(max_leads=2)
    assert len(data.leads) == 2 and data.truncated == {"leads": 1}


def test_requirements_property_and_transaction_type_are_projected():
    _, lead_pk = make_lead(
        status="qualifying", property_ref="P-1", preferred_location="New Cairo",
        metadata_json={"requirements": {"transaction_type": "rent", "finishing": "full"}, "qualification": {}},
    )
    with session_scope() as session:
        session.add(db.Property(property_id="P-1", compound="Mivida", transaction_type="sale", district="New Cairo"))
    row = row_for(load(), lead_pk)
    assert row.transaction_type == "rent"  # what the customer asked for beats the listing's own type
    assert row.compound == "Mivida" and row.requirements == "finishing: full" and row.qualified is False


# ---------------------------------------------------------------------------
# Tasks, handoffs, viewings
# ---------------------------------------------------------------------------


def test_tasks_the_next_task_and_recent_history():
    customer_pk, lead_pk = make_lead(name="Sara", phone="+201000000010")
    with session_scope() as session:
        session.add_all(
            [
                db.CrmTask(lead_id=lead_pk, task_type="call_customer", description="Later call", due_at=NOW + timedelta(days=3)),
                db.CrmTask(lead_id=lead_pk, task_type="confirm_viewing", description="Confirm the viewing", due_at=NOW + timedelta(days=1), priority="urgent"),
                db.CrmTask(lead_id=lead_pk, task_type="other", description="Done last week", status="done", completed_at=NOW - timedelta(days=7)),
                db.CrmTask(lead_id=lead_pk, task_type="other", description="Done long ago", status="done", updated_at=NOW - timedelta(days=60)),
            ]
        )
    data = load()
    assert {t.description for t in data.tasks} == {"Later call", "Confirm the viewing", "Done last week"}
    assert [t.status for t in data.tasks][:2] == ["open", "open"] and data.tasks[0].description == "Confirm the viewing"  # active first, soonest due first
    assert row_for(data, lead_pk).next_task == "Confirm the viewing"
    assert data.tasks[0].customer == "Sara" and data.tasks[0].phone == "+201000000010"


def test_an_open_handoff_is_flagged_and_a_resolved_one_sets_the_last_contact():
    customer_pk, flagged = make_lead()
    resolved_at = NOW - timedelta(days=2)
    other_customer, contacted = make_lead()
    with session_scope() as session:
        session.add(db.Handoff(customer_id=customer_pk, lead_id=flagged, status="claimed", reason="wants a person", priority="high"))
        session.add(db.Handoff(customer_id=other_customer, lead_id=contacted, status="resolved", reason="asked", priority="normal", resolved_at=resolved_at))
    data = load()
    assert row_for(data, flagged).open_handoff is True and row_for(data, contacted).open_handoff is False
    derived = row_for(data, contacted)
    assert derived.last_contact_at == resolved_at and derived.stored_last_contact_at is None  # shown, but not stored


def test_a_staff_entered_last_contact_wins_over_the_handoff():
    customer_pk, lead_pk = make_lead(last_contacted_at=NOW - timedelta(days=1))
    with session_scope() as session:
        session.add(db.Handoff(customer_id=customer_pk, lead_id=lead_pk, status="resolved", reason="x", priority="normal", resolved_at=NOW - timedelta(days=9)))
    assert row_for(load(), lead_pk).last_contact_at == NOW - timedelta(days=1)


def test_next_viewing_ignores_cancelled_and_past_bookings():
    customer_pk, lead_pk = make_lead(phone="+201000000011")

    def booking(status, starts, property_id):
        return db.Booking(property_id=property_id, customer_id=customer_pk, lead_id=lead_pk, starts_at=starts, ends_at=starts + timedelta(hours=1), status=status, contact_phone="+201000000011")

    soon = NOW + timedelta(days=2)
    with session_scope() as session:
        session.add_all([booking("confirmed", soon, "A"), booking("cancelled", NOW + timedelta(days=1), "B"), booking("confirmed", NOW - timedelta(days=3), "C")])
    data = load()
    assert row_for(data, lead_pk).next_viewing_at == soon
    assert {v.property_id for v in data.viewings} == {"A", "B", "C"} and all(v.lead_pk == lead_pk for v in data.viewings)


def test_a_booking_without_a_lead_id_is_still_tied_to_the_customers_lead():
    """The booking flow never sets bookings.lead_id; the export has to work that out."""
    customer_pk, lead_pk = make_lead(phone="+201000000012")
    soon = NOW + timedelta(days=2)
    with session_scope() as session:
        session.add(db.Booking(property_id="A", customer_id=customer_pk, starts_at=soon, ends_at=soon + timedelta(hours=1),
                               status="pending_confirmation", contact_phone="+201000000012"))
    data = load()
    assert row_for(data, lead_pk).next_viewing_at == soon
    assert [v.lead_pk for v in data.viewings] == [lead_pk]


def test_a_booking_is_tied_to_the_lead_that_recorded_its_appointment_or_was_current_at_the_time():
    customer_pk, first = make_lead(status="lost", created_at=NOW - timedelta(days=40))
    with session_scope() as session:
        second = db.Lead(customer_id=customer_pk, status="qualifying", source="website", created_at=NOW - timedelta(days=5))
        session.add(second)
        session.flush()
        second_pk = second.id
        early = db.Booking(property_id="EARLY", customer_id=customer_pk, starts_at=NOW + timedelta(days=1), ends_at=NOW + timedelta(days=1, hours=1),
                           status="confirmed", contact_phone="+20100", created_at=NOW - timedelta(days=30))  # made while only the first lead existed
        recorded = db.Booking(property_id="RECORDED", customer_id=customer_pk, starts_at=NOW + timedelta(days=3), ends_at=NOW + timedelta(days=3, hours=1),
                              status="confirmed", contact_phone="+20100", created_at=NOW - timedelta(days=2))
        latest = db.Booking(property_id="LATEST", customer_id=customer_pk, starts_at=NOW + timedelta(days=4), ends_at=NOW + timedelta(days=4, hours=1),
                            status="confirmed", contact_phone="+20100", created_at=NOW - timedelta(days=1))
        session.add_all([early, recorded, latest])
        session.flush()
        session.get(db.Lead, first).metadata_json = {"appointment_id": str(recorded.public_id)}  # the lead says this booking is its own
    by_property = {v.property_id: v.lead_pk for v in load().viewings}
    assert by_property == {"EARLY": first, "RECORDED": first, "LATEST": second_pk}


# ---------------------------------------------------------------------------
# Possible duplicates
# ---------------------------------------------------------------------------


def test_the_same_phone_on_two_channels_is_suggested_but_never_linked_by_a_website_number():
    phone = "+201012345678"
    web_customer, web_lead = make_lead("web:abc", phone=phone, verified=True, method="readback", name="Web visitor")
    wa_customer, wa_lead = make_lead("whatsapp:201012345678", phone=phone, verified=True, method="whatsapp_sender", source="whatsapp", name="WA sender")
    data = load()
    (pair,) = data.duplicates
    assert (pair.customer_a_pk, pair.customer_b_pk) == tuple(sorted((web_customer, wa_customer)))
    assert pair.evidence == "same_phone" and pair.decision == "" and pair.a_verified != pair.b_verified
    assert row_for(data, web_lead).possible_duplicate and row_for(data, wa_lead).possible_duplicate
    assert row_for(data, web_lead).person_key == "" and row_for(data, wa_lead).person_key == ""


def test_staff_decisions_group_or_dismiss_the_pair():
    phone = "+201012345679"
    a, lead_a = make_lead("web:one", phone=phone)
    b, lead_b = make_lead("whatsapp:201012345679", phone=phone, source="whatsapp")
    low, high = sorted((a, b))

    with session_scope() as session:
        session.add(db.CrmDuplicateReview(customer_a_id=low, customer_b_id=high, decision="confirmed", evidence="same_phone", reviewed_by="Mona"))
    data = load()
    assert row_for(data, lead_a).person_key == row_for(data, lead_b).person_key == f"P-{low:06d}"
    (pair,) = data.duplicates
    assert pair.decision == "confirmed" and pair.reviewed_by == "Mona" and pair.reviewed_at is not None

    with session_scope() as session:
        session.query(db.CrmDuplicateReview).update({"decision": "rejected"})
    data = load()
    assert row_for(data, lead_a).person_key == "" and not row_for(data, lead_a).possible_duplicate
    assert [p.decision for p in data.duplicates] == ["rejected"]  # kept visible so the decision can be changed


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_loading_the_export_changes_nothing():
    _, lead_pk = make_lead(name="Reader")
    with session_scope() as session:
        before = (session.query(db.Lead).count(), session.query(db.CrmTask).count(), session.get(db.Lead, lead_pk).updated_at)
    load()
    with session_scope() as session:
        after = (session.query(db.Lead).count(), session.query(db.CrmTask).count(), session.get(db.Lead, lead_pk).updated_at)
    assert before == after


def test_a_full_export_from_real_rows_opens_and_verifies(service, thread_id, stub_rag):
    say(service, thread_id, "villa in Sheikh Zayed, my budget is 9 million, for investment, within 3 months")
    blob = build_workbook(load(), agents=["Mona"], timezone="Africa/Cairo", signing_key=KEY)
    book = load_workbook(io.BytesIO(blob))
    assert book.sheetnames[:3] == ["Guide", "Dashboard", "Leads"]
    meta = read_meta(book)
    verify_meta(meta, KEY)
    (lead_uuid,) = meta.baseline("lead")
    assert book["Leads"].cell(row=2, column=sc.column_index("Leads", "lead_uuid")).value == lead_uuid
