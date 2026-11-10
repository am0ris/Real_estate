"""Regression tests built from a real WhatsApp conversation that went wrong.

Each test names the symptom the customer saw. They exercise the router, the property workflow, the
plain-text listing and the WhatsApp message splitter directly, so they need no network or provider.
"""

from __future__ import annotations

from app.agents.listing_text import format_property_list
from app.agents.state import describe_requirements, router
from app.agents.subgraphs.property.state import run_property_workflow
from app.agents.understanding import extract_requirements
from app.channels.whatsapp import split_message
from app.rag.generation import build_messages


def _listing(property_id, price, area, district, bedrooms=3, kind="Apartment", location=None):
    return {
        "property_id": property_id,
        "text": f"{kind} for sale in {district}",
        "score": 0.5,
        "metadata": {
            "property_id": property_id, "property_type": kind, "price": price, "area": area,
            "district": district, "bedrooms": bedrooms, "location": location or district,
        },
    }


SHOWN = [
    _listing("501188838", 8_700_000, 160, "New Heliopolis", location="Sodic East"),
    _listing("501188377", 8_000_000, 170, "New Cairo", location="5th Settlement"),
    _listing("501187869", 4_874_000, 165, "New Capital City", location="Al Maqsad"),
    _listing("501188450", 7_300_000, 125, "6th of October", location="Kayan"),
    _listing("501188214", 4_874_000, 165, "New Capital City"),  # the same unit listed twice
]
IDS = [item["property_id"] for item in SHOWN]


# --- "ميزانيتك البالغة 501,188,214 جنيه" / "حجزت لك معاينة" ---------------------------------------


def test_a_listing_id_typed_back_is_a_reference_not_a_budget():
    update = router({"query": "501188214", "candidates": SHOWN, "requirements": {"bedrooms": 3}})
    assert "max_price" not in update["requirements"]
    assert update["selected_property"]["property_id"] == "501188214"
    assert update["intent"] == "property_information"


def test_a_typed_id_does_not_break_the_booking_flow():
    update = router({"query": "501188214 العقار دة", "candidates": SHOWN, "scheduling": {"stage": "need_property"}})
    assert update["intent"] == "schedule_visit"
    assert update["selected_property"]["property_id"] == "501188214"
    assert "max_price" not in update["requirements"]


def test_long_digit_runs_are_money_only_with_money_wording():
    assert extract_requirements("501188999") == {}
    assert extract_requirements("100000000 جنيه")["max_price"] == 100_000_000
    assert extract_requirements("budget 120000000")["max_price"] == 120_000_000
    assert extract_requirements("5000000")["max_price"] == 5_000_000  # an ordinary budget still works


# --- pasting our own answer back / "اعملي مقارنة" ------------------------------------------------


def test_a_pasted_listing_line_is_not_a_new_search():
    line = "- شقة في New Capital City، مساحة 165 م²، السعر 4,874,000 EGP."
    update = router({"query": line, "candidates": SHOWN, "requirements": {"bedrooms": 3}})
    assert update["requirements"] == {"bedrooms": 3}  # no district / price taken from the pasted text
    assert update["intent"] == "property_information"
    assert update["selected_property"]["property_id"] in {"501187869", "501188214"}


def test_compare_two_pasted_listings():
    text = (
        "اعملي مقارنة بين - شقة في New Cairo، مساحة 170 م²، السعر 8,000,000 EGP.\n"
        "و - شقة في Sheraton، مساحة 160 م²، السعر 8,700,000 EGP."
    )
    update = router({"query": text, "candidates": SHOWN, "requirements": {"bedrooms": 3}})
    assert update["compare_ids"] == ["501188838", "501188377"]
    assert update["requirements"] == {"bedrooms": 3}
    assert update["intent"] == "property_information"


def test_compare_them_means_everything_just_shown():
    update = router({"query": "اعملي مقارنة بينهم", "candidates": SHOWN})
    assert update["compare_ids"] == IDS[:4]
    assert router({"query": "الأول والتاني", "candidates": SHOWN})["compare_ids"] == IDS[:2]
    assert router({"query": "ممكن 4 غرف", "candidates": SHOWN})["compare_ids"] == []  # not a comparison


def test_fifth_settlement_is_a_place_not_the_fifth_listing():
    update = router({"query": "طب في التجمع الخامس", "candidates": SHOWN})
    assert update["requirements"]["district"] == "New Cairo"
    assert update["selected_property"] is None
    assert update["intent"] == "property_search"
    assert router({"query": "الخامس", "candidates": SHOWN})["selected_property"]["property_id"] == "501188214"


# --- "ب اي سعر" / "ابدا من ديد" / "سجلت: ميزانية حتى 8,000,000" -----------------------------------


def test_any_price_lifts_the_budget_set_earlier():
    update = router(
        {"query": "عايز فيلا في القاهرة 4 غرف ب اي سعر",
         "requirements": {"max_price": 5_288_000.0, "district": "New Capital City"}}
    )
    assert "max_price" not in update["requirements"]
    assert update["requirements"]["property_type"] == "Villa"
    assert update["requirements"]["bedrooms"] == 4
    assert update["intent"] == "property_search"


def test_start_over_survives_typos_and_forgets_the_old_search_profile():
    for text in ("ابدا من ديد", "ابدأ من جدييد", "ابدأ تاني"):
        update = router(
            {"query": text, "requirements": {"district": "New Cairo"}, "candidates": SHOWN,
             "qualification": {"budget": 8_000_000, "preferred_location": "New Cairo", "purpose": "residence"}}
        )
        assert update["requirements"] == {} and update["candidates"] == [], text
        assert update["intent"] == "search_reset", text
        assert update["qualification"] == {"purpose": "residence"}, text  # who they are stays, what they searched goes


def test_the_summary_is_arabic_and_shows_only_the_budget_the_search_applies():
    summary = describe_requirements(
        {"property_type": "Villa", "district": "New Capital City", "bedrooms": 4},
        {"budget": 8_000_000, "purpose": "residence"},
        "ar",
    )
    assert "فيلا" in summary and "العاصمة الإدارية" in summary and "للسكن" in summary
    assert "Villa" not in summary and "ميزانية" not in summary


# --- an empty search wiped the list we had just shown --------------------------------------------


def _fake_rag(monkeypatch, *, properties, calls):
    def answer_query(query, requirements=None, conversation_history=None, **kwargs):
        calls.append(("search", kwargs))
        return {"answer": "ملقتش عقارات مطابقة.", "properties": properties,
                "cited_property_ids": [], "grounding_property_ids": []}

    def answer_about_properties(query, props, requirements=None, conversation_history=None, **kwargs):
        calls.append(("about", [p["property_id"] for p in props]))
        return {"answer": "مقارنة", "properties": props, "cited_property_ids": [], "grounding_property_ids": []}

    monkeypatch.setattr("app.workflows.rag.answer_query", answer_query)
    monkeypatch.setattr("app.workflows.rag.answer_about_properties", answer_about_properties)


def test_an_empty_search_keeps_the_listings_the_customer_was_shown(monkeypatch):
    _fake_rag(monkeypatch, properties=[], calls=[])
    result = run_property_workflow(
        {"query": "فيلا 4 غرف", "language": "ar", "intent": "property_search", "candidates": SHOWN,
         "selected_property": SHOWN[0], "requirements": {"property_type": "Villa", "bedrooms": 4}}
    )
    assert "candidates" not in result and "selected_property" not in result  # untouched
    assert "كان بحثي عن" in result["response"] and "فيلا" in result["response"]  # says what was searched


def test_a_new_non_empty_search_replaces_the_list(monkeypatch):
    _fake_rag(monkeypatch, properties=SHOWN[:2], calls=[])
    result = run_property_workflow(
        {"query": "شقة", "language": "ar", "intent": "property_search", "candidates": SHOWN[2:],
         "requirements": {"property_type": "Apartment"}}
    )
    assert result["candidates"] == SHOWN[:2] and result["selected_property"] is None


def test_comparison_runs_on_exactly_the_listings_named(monkeypatch):
    calls = []
    _fake_rag(monkeypatch, properties=[], calls=calls)
    result = run_property_workflow(
        {"query": "قارن", "language": "ar", "intent": "property_information", "candidates": SHOWN,
         "compare_ids": ["501188377", "501188838"], "requirements": {}}
    )
    assert calls == [("about", ["501188838", "501188377"])]  # no fresh search
    assert result["response"] == "مقارنة"


# --- WhatsApp has no property cards ------------------------------------------------------------


def test_whatsapp_gets_the_listings_as_text_and_the_website_does_not(monkeypatch):
    calls = []
    _fake_rag(monkeypatch, properties=SHOWN[:2], calls=calls)
    state = {"query": "شقة", "language": "ar", "intent": "property_search", "requirements": {"property_type": "Apartment"}}

    whatsapp = run_property_workflow({**state, "channel": "whatsapp"})
    assert "1) شقة" in whatsapp["response"] and "كود 501188838" in whatsapp["response"]
    assert "2) شقة" in whatsapp["response"] and "كود 501188377" in whatsapp["response"]
    assert calls[-1] == ("search", {"channel": "whatsapp"})  # the model is told there are no cards

    website = run_property_workflow({**state, "channel": "website"})
    assert website["response"] == "ملقتش عقارات مطابقة."  # cards carry the details there
    assert calls[-1] == ("search", {})


def test_the_text_list_is_built_from_stored_data():
    text = format_property_list(SHOWN[:2] + [_listing("1", 13_200_000, 10, "Sheraton")], "ar")
    assert "1) شقة في Sodic East، هليوبوليس الجديدة — 160 م² — 3 غرف — 8,700,000 جنيه — كود 501188838" in text
    assert "10 م²" not in text  # an implausible area is not shown as fact
    assert format_property_list([], "ar") == ""
    assert format_property_list(SHOWN, "en", limit=2).count(") Apartment") == 2


def test_the_whatsapp_prompt_does_not_point_at_cards_that_do_not_exist():
    candidates = []
    standard = build_messages("x", candidates, {}, None)[0]["content"]
    text_only = build_messages("x", candidates, {}, None, channel="whatsapp")[0]["content"]
    assert "displays retrieved properties separately" in standard
    assert "displays retrieved properties separately" not in text_only and "NO property cards" in text_only
    for prompt in (standard, text_only):
        assert "booked or confirmed" in prompt  # never claim a viewing is booked


def test_long_replies_are_split_not_cut():
    assert split_message("hello") == ["hello"] and split_message("  ") == []
    long_text = ("فقرة طويلة " * 120 + "\n\n") * 6
    parts = split_message(long_text)
    assert len(parts) > 1 and all(len(part) <= 3800 for part in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == long_text.replace(" ", "").replace("\n", "")


def test_a_unit_listed_twice_is_shown_once_and_conditions_in_force_are_visible(monkeypatch):
    twin = _listing("501188999", 4_874_000, 165, "New Capital City")  # same unit as 501187869, other id
    _fake_rag(monkeypatch, properties=[SHOWN[2], twin, SHOWN[1]], calls=[])
    result = run_property_workflow(
        {"query": "شقة", "language": "ar", "intent": "property_search", "channel": "whatsapp",
         "requirements": {"property_type": "Apartment", "district": "New Capital City", "max_price": 10_000_000.0}}
    )
    assert [c["property_id"] for c in result["candidates"]] == ["501187869", "501188377"]
    assert "1) " in result["response"] and "2) " in result["response"] and "3) " not in result["response"]
    assert 'الشروط الحالية: شقة، في العاصمة الإدارية، ميزانية حتى 10,000,000 جنيه' in result["response"]
    assert 'ابدأ من جديد' in result["response"]


def test_the_first_two_means_exactly_two():
    assert router({"query": "Compare the first two", "candidates": SHOWN})["compare_ids"] == IDS[:2]
    assert router({"query": "قارن اول اتنين", "candidates": SHOWN})["compare_ids"] == IDS[:2]
    assert router({"query": "compare the first three", "candidates": SHOWN})["compare_ids"] == IDS[:3]
    assert router({"query": "Compare them", "candidates": SHOWN})["compare_ids"] == IDS[:4]


def test_a_new_kind_of_property_in_a_new_place_starts_a_new_search():
    carried = {"property_type": "Apartment", "district": "New Cairo", "bedrooms": 3, "max_price": 8_000_000.0}
    update = router({"query": "Now show me villas in Sheikh Zayed", "requirements": carried})
    assert update["requirements"] == {"property_type": "Villa", "district": "Sheikh Zayed"}
    update = router({"query": "طيب وريني فيلل في الشيخ زايد", "requirements": {"property_type": "Apartment", "district": "New Cairo", "bedrooms": 3}})
    assert update["requirements"] == {"property_type": "Villa", "district": "Sheikh Zayed"}


def test_a_refinement_keeps_what_was_already_asked():
    carried = {"property_type": "Apartment", "district": "New Cairo", "bedrooms": 3, "max_price": 8_000_000.0}
    # only the place changes: same kind of property, so bedrooms and budget stay
    kept = router({"query": "apartment in Sheikh Zayed", "requirements": carried})["requirements"]
    assert kept["district"] == "Sheikh Zayed" and kept["bedrooms"] == 3 and kept["max_price"] == 8_000_000.0
    # "same budget" keeps the context even when both change
    same = router({"query": "villas in Sheikh Zayed, same budget", "requirements": carried})["requirements"]
    assert same["property_type"] == "Villa" and same["max_price"] == 8_000_000.0
