"""CRM workbook, step 1: schema additions, status protection and settings.

The new columns and tables must be inert for the existing assistant: nothing
writes them yet, the old status rules still hold, and the two new protections
(a stage set by sales staff is never pulled back, a closed lead is final for
automatic updates) are covered here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.agents.tools.crm import _STATUS_RANK, crm_service
from app.database import models as db
from app.database.session import session_scope
from app.helpers.config import Settings


def _new_lead(thread_id: str | None = None) -> tuple[str, int]:
    """Create a customer+lead through the real service; return (external_id, lead pk)."""
    external_id = thread_id or f"web:{uuid4().hex}"
    crm_service.ensure_lead(external_id, source="website")
    with session_scope() as session:
        customer = session.query(db.Customer).filter_by(external_id=external_id).one()
        lead = session.query(db.Lead).filter_by(customer_id=customer.id).one()
        return external_id, lead.id


def _customer_pk(external_id: str) -> int:
    with session_scope() as session:
        return session.query(db.Customer).filter_by(external_id=external_id).one().id


def _status(external_id: str) -> str:
    return crm_service.get_lead(external_id)["status"]


def _set_status(lead_pk: int, status: str) -> None:
    with session_scope() as session:
        session.get(db.Lead, lead_pk).status = status


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_new_lead_columns_start_empty():
    _, lead_pk = _new_lead()
    with session_scope() as session:
        lead = session.get(db.Lead, lead_pk)
        assert lead.assigned_agent is None
        assert lead.last_contacted_at is None
        assert lead.next_followup_at is None


def test_task_defaults_and_unique_dedupe_key():
    _, lead_pk = _new_lead()
    with session_scope() as session:
        session.add(db.CrmTask(lead_id=lead_pk, task_type="call_customer", dedupe_key="handoff:1:call"))
        session.add(db.CrmTask(lead_id=lead_pk, task_type="other"))  # NULL keys never collide
        session.add(db.CrmTask(lead_id=lead_pk, task_type="other"))
    with session_scope() as session:
        task = session.query(db.CrmTask).filter_by(dedupe_key="handoff:1:call").one()
        assert (task.status, task.priority, task.source, task.description) == ("open", "normal", "system", "")
        assert task.public_id is not None and task.created_at is not None

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(db.CrmTask(lead_id=lead_pk, task_type="call_customer", dedupe_key="handoff:1:call"))


@pytest.mark.parametrize("field,value", [("status", "bogus"), ("priority", "urgentish"), ("source", "robot")])
def test_task_rejects_values_outside_the_allowed_sets(field, value):
    _, lead_pk = _new_lead()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(db.CrmTask(lead_id=lead_pk, task_type="other", **{field: value}))


def test_duplicate_review_needs_an_ordered_pair_and_a_valid_decision():
    first, _ = _new_lead()
    second, _ = _new_lead()
    low, high = sorted((_customer_pk(first), _customer_pk(second)))

    with session_scope() as session:
        session.add(db.CrmDuplicateReview(customer_a_id=low, customer_b_id=high, decision="rejected", evidence="same_phone"))

    for pair, decision in (((high, low), "confirmed"), ((low, high), "maybe")):
        with pytest.raises(IntegrityError):
            with session_scope() as session:
                session.add(db.CrmDuplicateReview(customer_a_id=pair[0], customer_b_id=pair[1], decision=decision))

    with pytest.raises(IntegrityError):  # one decision per pair
        with session_scope() as session:
            session.add(db.CrmDuplicateReview(customer_a_id=low, customer_b_id=high, decision="confirmed"))


def test_deleting_a_lead_removes_its_tasks():
    _, lead_pk = _new_lead()
    with session_scope() as session:
        session.add(db.CrmTask(lead_id=lead_pk, task_type="other"))
    with session_scope() as session:
        session.query(db.Lead).filter_by(id=lead_pk).delete()
    with session_scope() as session:
        assert session.query(db.CrmTask).count() == 0


# ---------------------------------------------------------------------------
# Status protection
# ---------------------------------------------------------------------------


def test_negotiation_outranks_every_automatic_status():
    automatic = {key: rank for key, rank in _STATUS_RANK.items() if key != "negotiation"}
    assert _STATUS_RANK["negotiation"] > max(automatic.values())
    # record_handoff demotes only leads ranked at or below handoff_requested.
    assert _STATUS_RANK["negotiation"] > _STATUS_RANK["handoff_requested"]


@pytest.mark.parametrize("automatic", ["new", "qualifying", "hot", "viewing_requested", "viewing_booked", "handoff_requested", "contacted"])
def test_an_automatic_update_cannot_pull_a_lead_out_of_negotiation(automatic):
    external_id, lead_pk = _new_lead()
    _set_status(lead_pk, "negotiation")
    crm_service.update_lead(external_id, {"status": automatic})
    assert _status(external_id) == "negotiation"


@pytest.mark.parametrize("final", ["won", "lost", "closed"])
def test_a_closed_lead_is_final_for_automatic_updates(final):
    external_id, lead_pk = _new_lead()
    _set_status(lead_pk, final)
    crm_service.update_lead(external_id, {"status": "qualifying"})
    crm_service.update_lead(external_id, {"status": "hot"})
    assert _status(external_id) == final


def test_other_fields_still_update_on_a_closed_lead():
    external_id, lead_pk = _new_lead()
    _set_status(lead_pk, "lost")
    crm_service.update_lead(external_id, {"preferred_location": "New Cairo", "status": "qualifying"})
    with session_scope() as session:
        lead = session.get(db.Lead, lead_pk)
        assert lead.status == "lost" and lead.preferred_location == "New Cairo"


def test_an_open_lead_can_still_be_closed_automatically():
    external_id, _ = _new_lead()
    crm_service.update_lead(external_id, {"status": "won"})
    assert _status(external_id) == "won"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_crm_agents_are_trimmed_deduplicated_and_ordered():
    settings = Settings(CRM_AGENTS=" Ahmed ,Mona,  ahmed,, Sara  Ali ")
    assert settings.crm_agents == ["Ahmed", "Mona", "Sara Ali"]
    assert Settings(CRM_AGENTS="").crm_agents == []


def test_blank_crm_keys_count_as_unset():
    settings = Settings(CRM_API_KEY="  ", CRM_SIGNING_KEY='""')
    assert settings.CRM_API_KEY is None and settings.CRM_SIGNING_KEY is None


def test_a_short_crm_api_key_is_a_production_problem():
    weak = Settings(CRM_API_KEY="short")
    assert any("CRM_API_KEY" in problem for problem in weak.production_problems())
    strong = Settings(CRM_API_KEY="k" * 32)
    assert not any("CRM_API_KEY" in problem for problem in strong.production_problems())


def test_the_new_setting_did_not_change_how_existing_properties_behave():
    """Guards the edit itself: a stray decorator once turned staff_channels into a method."""
    for name in ("crm_agents", "staff_channels", "cors_origins"):
        assert isinstance(getattr(Settings, name), property), name
    assert Settings().staff_channels == [] and Settings(CRM_AGENTS="").crm_agents == []
