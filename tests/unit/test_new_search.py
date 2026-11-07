"""WhatsApp keeps one conversation per customer for weeks, so a search carried over from an old
message must be removable in plain words ("start over"), and a stale filter must never trap the
customer. The website has a reset button and filter chips for the same purpose."""

from __future__ import annotations

from app.agents.state import router
from app.agents.subgraphs.property.state import run_property_workflow, search_filters
from app.agents.understanding import wants_new_search


def test_start_over_wording_is_recognised():
    for text in (
        "ابدأ من جديد",
        "ابدا من الاول",
        "عايز بحث جديد",
        "امسح البحث",
        "start over",
        "New search please",
        "reset my search",
        "reset",
    ):
        assert wants_new_search(text), text


def test_ordinary_messages_do_not_reset_the_search():
    for text in (
        "ابعتلي الصور من جديد",
        "شقة جديدة في التجمع",
        "عايز شقة 3 غرف",
        "I want a new apartment in New Cairo",
        "can you reset the price to 5 million",
        "ok",
    ):
        assert not wants_new_search(text), text


def test_start_over_clears_everything_carried_over():
    stale = {
        "query": "ابدأ من جديد",
        "requirements": {"district": "New Capital", "bedrooms": 3, "max_price": 3_000_000},
        "candidates": [{"property_id": "1"}],
        "selected_property": {"property_id": "1"},
        "scheduling": {"stage": "need_date"},
    }
    update = router(stale)
    assert update["requirements"] == {}
    assert update["candidates"] == []
    assert update["selected_property"] is None
    assert update["scheduling"] == {}
    assert update["intent"] == "search_reset"  # a short fixed prompt, not a questionnaire


def test_start_over_with_new_details_keeps_only_the_new_ones():
    update = router(
        {
            "query": "ابدأ من جديد، فيلا في الشيخ زايد",
            "requirements": {"district": "New Cairo", "bedrooms": 3},
            "candidates": [{"property_id": "1"}],
        }
    )
    assert update["requirements"] == {"property_type": "Villa", "district": "Sheikh Zayed"}
    assert update["intent"] == "property_search"
    assert update["candidates"] == []


def test_an_ordinary_follow_up_keeps_the_carried_search():
    update = router({"query": "ممكن 4 غرف", "requirements": {"district": "New Cairo"}})
    assert update["requirements"]["district"] == "New Cairo"
    assert update["requirements"]["bedrooms"] == 4
    assert "candidates" not in update


def test_buying_wording_is_understood_but_never_filters_the_search():
    update = router({"query": "عايز اشتري شقة في التجمع الخامس"})
    assert update["requirements"]["transaction_type"] == "sale"  # still recorded for the CRM
    assert search_filters(update["requirements"]) == {"property_type": "Apartment", "district": "New Cairo"}


def test_rent_request_is_answered_without_searching(monkeypatch):
    def must_not_search(*args, **kwargs):
        raise AssertionError("a rent request must not run a sale search")

    monkeypatch.setattr("app.workflows.rag.answer_query", must_not_search)
    result = run_property_workflow(
        {
            "query": "عايز شقة للايجار في التجمع",
            "language": "ar",
            "intent": "property_search",
            "requirements": {"transaction_type": "rent", "district": "New Cairo"},
        }
    )
    assert "للبيع" in result["response"]
    assert "error" not in result


def test_an_old_rent_request_does_not_block_a_later_search(monkeypatch):
    seen = {}

    def fake_answer_query(query, filters, conversation_history=None):
        seen["filters"] = filters
        return {"answer": "ok", "properties": [{"property_id": "1"}], "cited_property_ids": [], "grounding_property_ids": []}

    monkeypatch.setattr("app.workflows.rag.answer_query", fake_answer_query)
    result = run_property_workflow(
        {
            "query": "شقة في التجمع",
            "language": "ar",
            "intent": "property_search",
            "requirements": {"transaction_type": "rent", "district": "New Cairo", "property_type": "Apartment"},
        }
    )
    assert result["response"] == "ok"
    assert seen["filters"] == {"district": "New Cairo", "property_type": "Apartment"}
