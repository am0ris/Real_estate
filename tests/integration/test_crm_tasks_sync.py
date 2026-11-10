"""System follow-up tasks: what creates them, what closes them, and that repeating the sync is harmless."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.crm.excel import tasks as task_module
from app.crm.excel.tasks import HOT_SCORE, sync_system_tasks
from app.database import models as db
from app.database.session import session_scope
from app.leads.scoring import score_band
from tests.integration.test_crm_excel_queries import make_lead

TZ = "Africa/Cairo"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def sync(now=NOW, **kwargs):
    with session_scope() as session:
        return sync_system_tasks(session, now=now, timezone=TZ, **kwargs)


def tasks_of(lead_pk):
    with session_scope() as session:
        rows = session.query(db.CrmTask).filter_by(lead_id=lead_pk).order_by(db.CrmTask.id).all()
        return [
            {"key": t.dedupe_key, "type": t.task_type, "status": t.status, "priority": t.priority, "source": t.source,
             "description": t.description, "agent": t.assigned_agent, "due": t.due_at, "completed": t.completed_at}
            for t in rows
        ]


def add_handoff(customer_pk, lead_pk, **fields):
    with session_scope() as session:
        handoff = db.Handoff(customer_id=customer_pk, lead_id=lead_pk, status=fields.pop("status", "open"),
                             reason=fields.pop("reason", "asked for a person"), priority=fields.pop("priority", "high"), **fields)
        session.add(handoff)
        session.flush()
        return handoff.id


def add_booking(customer_pk, lead_pk=None, *, starts, status="pending_confirmation", property_id="P-1", **fields):
    """A booking the way the booking flow writes it: no lead_id (the link is resolved when reading)."""
    with session_scope() as session:
        booking = db.Booking(property_id=property_id, customer_id=customer_pk, lead_id=lead_pk, starts_at=starts,
                             ends_at=starts + timedelta(hours=1), status=status, contact_phone="+201000000001", **fields)
        session.add(booking)
        session.flush()
        return booking.id


def set_status(model, pk, **values):
    with session_scope() as session:
        row = session.get(model, pk)
        for key, value in values.items():
            setattr(row, key, value)


def add_message(customer_pk, lead_pk, when):
    with session_scope() as session:
        session.add(db.Interaction(customer_id=customer_pk, lead_id=lead_pk, channel="website:text", content="hello", created_at=when))


# ---------------------------------------------------------------------------


def test_the_hot_threshold_follows_the_scoring_bands():
    assert HOT_SCORE == 70 and score_band(HOT_SCORE) == "hot" and score_band(HOT_SCORE - 1) != "hot"


def test_a_handoff_creates_one_urgent_call_task_that_closes_when_the_handoff_is_resolved():
    customer, lead = make_lead(assigned_agent="Mona")
    handoff = add_handoff(customer, lead)

    assert sync().created == 1
    (task,) = tasks_of(lead)
    assert task["key"] == f"handoff:{handoff}" and task["type"] == "call_customer" and task["priority"] == "high"
    assert task["status"] == "open" and task["source"] == "system" and task["agent"] == "Mona"
    assert "sales representative" in task["description"] and "موظف مبيعات" in task["description"] and "asked for a person" in task["description"]

    assert sync().created == 0 and len(tasks_of(lead)) == 1  # running it again changes nothing

    resolved_at = NOW + timedelta(hours=1)
    set_status(db.Handoff, handoff, status="resolved", resolved_at=resolved_at)
    result = sync()
    (task,) = tasks_of(lead)
    assert result.completed == 1 and task["status"] == "done" and task["completed"] == resolved_at


def test_an_urgent_handoff_keeps_its_priority():
    customer, lead = make_lead()
    add_handoff(customer, lead, priority="urgent")
    sync()
    assert tasks_of(lead)[0]["priority"] == "urgent"


def test_a_pending_viewing_creates_a_confirmation_task_in_cairo_time():
    customer, lead = make_lead()
    starts = datetime(2099, 11, 10, 10, 0, tzinfo=timezone.utc)  # 12:00 in Cairo (UTC+2)
    booking = add_booking(customer, starts=starts, property_id="501189810")
    sync()
    (task,) = tasks_of(lead)
    assert task["key"] == f"booking:{booking}:confirm" and task["type"] == "confirm_viewing" and task["priority"] == "high"
    assert "501189810" in task["description"] and "2099-11-10 12:00" in task["description"]


def test_viewings_in_the_past_or_already_decided_need_no_task():
    customer, lead = make_lead()
    add_booking(customer, starts=NOW - timedelta(days=1))
    add_booking(customer, starts=NOW + timedelta(days=2), status="confirmed")
    assert sync().created == 0 and tasks_of(lead) == []


@pytest.mark.parametrize("new_status,expected", [("confirmed", "done"), ("completed", "done"), ("cancelled", "cancelled"), ("expired", "cancelled")])
def test_the_confirmation_task_follows_the_booking(new_status, expected):
    customer, lead = make_lead()
    booking = add_booking(customer, starts=NOW + timedelta(days=2))
    sync()
    set_status(db.Booking, booking, status=new_status)
    sync()
    assert tasks_of(lead)[0]["status"] == expected


@pytest.mark.parametrize(
    "fields,gets_task",
    [
        ({"score": 80}, True), ({"score": HOT_SCORE}, True), ({"score": HOT_SCORE - 1}, False),
        ({"score": 80, "status": "contacted"}, False), ({"score": 80, "status": "negotiation"}, False),
        ({"score": 80, "status": "won"}, False), ({"score": 80, "status": "lost"}, False),
        ({"score": 80, "last_contacted_at": NOW - timedelta(days=1)}, False),
    ],
)
def test_only_hot_leads_nobody_has_contacted_get_a_task(fields, gets_task):
    _, lead = make_lead(**fields)
    sync(inactive_days=0)
    assert [t["key"] for t in tasks_of(lead)] == ([f"hot:{lead}"] if gets_task else [])


def test_a_hot_lead_already_being_called_does_not_get_a_second_call_task():
    customer, lead = make_lead(score=90)
    add_handoff(customer, lead)
    sync(inactive_days=0)
    assert [t["key"].split(":")[0] for t in tasks_of(lead)] == ["handoff"]


def test_the_hot_lead_task_closes_once_someone_contacts_the_customer():
    _, lead = make_lead(score=90)
    sync(inactive_days=0)
    set_status(db.Lead, lead, status="contacted")
    assert sync(inactive_days=0).completed == 1 and tasks_of(lead)[0]["status"] == "done"
    sync(inactive_days=0)
    assert len(tasks_of(lead)) == 1  # and it is not recreated


def test_a_quiet_lead_gets_a_reengage_task_only_when_nothing_else_explains_the_silence():
    _, quiet = make_lead(created_at=NOW - timedelta(days=10))
    customer, recent = make_lead(created_at=NOW - timedelta(days=10))
    add_message(customer, recent, NOW - timedelta(days=1))
    _, contacted = make_lead(created_at=NOW - timedelta(days=10), last_contacted_at=NOW - timedelta(days=2))
    customer, busy = make_lead(created_at=NOW - timedelta(days=10))
    with session_scope() as session:
        session.add(db.CrmTask(lead_id=busy, task_type="other", source="staff"))
    _, closed = make_lead(status="lost", created_at=NOW - timedelta(days=10))

    sync()
    quiet_day = (NOW - timedelta(days=10)).date().isoformat()
    assert [t["key"] for t in tasks_of(quiet)] == [f"inactive:{quiet}:{quiet_day}"]
    assert tasks_of(quiet)[0]["type"] == "reengage_inactive" and tasks_of(quiet)[0]["priority"] == "normal"
    assert [tasks_of(x) for x in (recent, contacted, closed)] == [[], [], []]
    assert [t["source"] for t in tasks_of(busy)] == ["staff"]
    assert sync(inactive_days=0).created == 0  # switched off


def test_a_customer_who_writes_back_cancels_the_reengage_task_and_a_new_silence_gets_a_new_one():
    customer, lead = make_lead(created_at=NOW - timedelta(days=20))
    sync()
    (first,) = tasks_of(lead)
    assert first["status"] == "open"

    add_message(customer, lead, NOW + timedelta(minutes=5))  # after the task was created
    sync(now=NOW + timedelta(minutes=10))
    assert tasks_of(lead)[0]["status"] == "cancelled"

    later = NOW + timedelta(days=9)  # quiet again for more than a week
    sync(now=later)
    keys = [t["key"] for t in tasks_of(lead)]
    assert len(keys) == 2 and keys[0] != keys[1] and tasks_of(lead)[1]["status"] == "open"


def test_closing_a_lead_cancels_its_system_tasks_and_leaves_staff_tasks_alone():
    customer, lead = make_lead()
    add_handoff(customer, lead)
    with session_scope() as session:
        session.add(db.CrmTask(lead_id=lead, task_type="send_details", source="staff", description="mine"))
    sync()
    set_status(db.Lead, lead, status="lost")
    result = sync()
    by_source = {t["source"]: t["status"] for t in tasks_of(lead)}
    assert result.cancelled == 1 and by_source == {"system": "cancelled", "staff": "open"}


def test_a_task_a_person_cancelled_is_never_recreated():
    customer, lead = make_lead()
    add_handoff(customer, lead)
    sync()
    with session_scope() as session:
        session.query(db.CrmTask).update({"status": "cancelled"})
    sync()
    sync()
    assert [t["status"] for t in tasks_of(lead)] == ["cancelled"]


def test_a_backlog_is_worked_off_over_several_runs_without_starving_old_leads(monkeypatch):
    monkeypatch.setattr(task_module, "MAX_NEW_PER_RULE", 2)
    leads = [make_lead(score=90)[1] for _ in range(5)]
    created = [sync(inactive_days=0).created for _ in range(4)]
    assert created == [2, 2, 1, 0]
    assert all(len(tasks_of(lead)) == 1 for lead in leads)


def test_the_sync_leaves_leads_and_conversations_untouched():
    customer, lead = make_lead(score=90, name="Reader")
    add_message(customer, lead, NOW)
    with session_scope() as session:
        before = (session.get(db.Lead, lead).status, session.get(db.Lead, lead).updated_at, session.query(db.Interaction).count())
    sync()
    with session_scope() as session:
        after = (session.get(db.Lead, lead).status, session.get(db.Lead, lead).updated_at, session.query(db.Interaction).count())
    assert before == after


def test_a_pending_viewing_of_a_customer_with_a_closed_old_lead_goes_to_their_current_lead():
    customer, old = make_lead(status="lost", created_at=NOW - timedelta(days=30))
    with session_scope() as session:
        current = db.Lead(customer_id=customer, status="viewing_requested", source="website", created_at=NOW - timedelta(days=1))
        session.add(current)
        session.flush()
        current_pk = current.id
    add_booking(customer, starts=NOW + timedelta(days=2))
    sync()
    assert tasks_of(old) == [] and [t["type"] for t in tasks_of(current_pk)] == ["confirm_viewing"]
