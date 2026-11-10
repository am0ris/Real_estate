"""Shared conversation state and the routing step of the agent graph.

One ``ConversationState`` is checkpointed per conversation thread, so the
requirements, qualification data, candidates and scheduling progress collected
by any agent — over text, voice or WhatsApp — stay available to every other
agent on later turns.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.helpers.locations import district_label
from app.helpers.phone import normalize_phone

from app.agents.understanding import (
    classify_intent,
    detect_language,
    extract_qualification,
    blank_known_ids,
    changes_topic,
    compare_targets,
    extract_requirements,
    match_pasted_listings,
    requirements_to_clear,
    resolve_property_reference,
    wants_new_search,
)


Channel = Literal[
    "website",
    "whatsapp",
]

Language = Literal[
    "ar",
    "en",
]


class ConversationState(TypedDict, total=False):


    messages: Annotated[list[BaseMessage], add_messages]

    query: str
    rewritten_query: str

    channel: Channel
    language: Language
    # "text" or "voice" — voice replies are spoken, so agents keep them short.
    modality: str

    # --------------------------------------------------------
    # Routing
    # --------------------------------------------------------

    intent: str
    next_agent: str
    active_agent: str
    # Property ids of the listings to compare this turn (empty on every other turn).
    compare_ids: list[str]

    # --------------------------------------------------------
    # Customer
    # --------------------------------------------------------

    customer_id: str
    customer: dict

    # --------------------------------------------------------
    # Property
    # --------------------------------------------------------

    requirements: dict
    # Requirement keys the client asked to drop this turn (filter chips).
    clear_requirements: list[str]
    candidates: list[dict]
    selected_property: dict | None

    # --------------------------------------------------------
    # Qualification
    # --------------------------------------------------------

    qualification: dict
    lead_id: str | None

    # --------------------------------------------------------
    # Scheduling
    # --------------------------------------------------------

    scheduling: dict
    appointment: dict | None

    # --------------------------------------------------------
    # Handoff
    # --------------------------------------------------------

    handoff: dict | None

    # --------------------------------------------------------
    # Per-turn output (reset by the conversation service each turn)
    # --------------------------------------------------------

    response: str
    turn_properties: list[dict]
    cited_property_ids: list[str]
    grounding_property_ids: list[str]
    error: str | None


# Scheduling stages during which short replies ("Saturday", "yes") continue the booking.
SCHEDULING_STAGES = {
    "need_property", "need_date", "choose_slot", "need_contact", "verify_contact", "confirm",
    "cancel_pick", "cancel_confirm",
}

# Requirement keys translated into hard metadata filters by the RAG workflow.
# Every key here must be a field that the ingested payloads really carry (and index): a hard
# filter on a field no point has returns zero listings. ``transaction_type`` is deliberately
# absent because the catalogue has no such field (every listing is for sale); a customer asking
# to rent is answered in ``run_property_workflow``. Add it back only together with the field in
# the ingested payload, ``PAYLOAD_INDEXES`` and a backfill — ``tests/unit/test_search_filters.py``
# fails if a key here has no data behind it.
FILTER_KEYS = (
    "property_type",
    "district",
    "bedrooms",
    "min_bedrooms",
    "min_price",
    "max_price",
)


def merge_requirements(previous: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **new}
    if "bedrooms" in new:
        merged.pop("min_bedrooms", None)
    if "min_bedrooms" in new:
        merged.pop("bedrooms", None)
    if "max_price" in new and "min_price" not in new and merged.get("min_price", 0) >= new["max_price"]:
        merged.pop("min_price", None)
    if "min_price" in new and "max_price" not in new and merged.get("max_price", float("inf")) <= new["min_price"]:
        merged.pop("max_price", None)
    return merged


def merge_contact(previous: dict[str, Any], contact: dict[str, Any], state: ConversationState) -> dict[str, Any]:
    """Fold contact details from this message into the customer profile.

    Phones are stored in E.164 only. A number that cannot be parsed is *not*
    stored (``phone_invalid`` is set so the agent can say so), and any change of
    number clears its verification: verification belongs to one exact number.
    On WhatsApp the sender's own number is pre-verified by the channel.
    """
    customer = {**previous, **{key: value for key, value in contact.items() if key != "phone"}}

    raw_phone = contact.get("phone")
    if raw_phone:
        phone = normalize_phone(raw_phone)
        if phone is None:
            customer["phone_invalid"] = True
            customer.pop("phone", None)
            customer.pop("phone_verified", None)
        else:
            if phone != previous.get("phone"):
                customer["phone_verified"] = False
                customer.pop("phone_verification_method", None)
            customer["phone"] = phone
            customer.pop("phone_invalid", None)

    if not customer.get("phone") and str(state.get("customer_id") or "").startswith("whatsapp:"):
        sender = normalize_phone(str(state["customer_id"]).split(":", 1)[1])
        if sender:
            customer.update(phone=sender, phone_verified=True, phone_verification_method="whatsapp_sender")
    return customer


def router(state: ConversationState) -> dict:
    """Understand the latest message, update shared state and pick an intent.

    Runs on every turn before any specialised agent, so all agents see the
    same, accumulated customer requirements regardless of which one answers.
    """

    query = state.get("query", "")
    language = detect_language(query, state.get("language") or "en")

    # "Start over" wipes the carried search so an old filter can never trap the customer.
    fresh_search = wants_new_search(query)
    scheduling = {} if fresh_search else (state.get("scheduling") or {})
    candidates = [] if fresh_search else (state.get("candidates") or [])
    # Once a property is chosen for a viewing, "the first one" refers to time slots.
    picking_slot = scheduling.get("stage") in SCHEDULING_STAGES - {"need_property"}

    # A listing id typed back ("501188214") is a reference, never a price.
    known_ids = {str(c.get("property_id")) for c in candidates if c.get("property_id")}
    selected_id = (state.get("selected_property") or {}).get("property_id")
    if selected_id:
        known_ids.add(str(selected_id))
    understood = blank_known_ids(query, known_ids)

    # Listings the customer wants compared, or pasted back from our own answer.
    compare_with = [] if picking_slot else compare_targets(query, candidates)
    pasted = [] if picking_slot else match_pasted_listings(query, candidates)

    previous_requirements = {} if fresh_search else dict(state.get("requirements") or {})
    # Chips ("Without Sheikh Zayed") and wording ("any price") lift conditions set earlier.
    typed_clear = requirements_to_clear(query)
    to_clear = list(state.get("clear_requirements") or []) + typed_clear
    for key in to_clear:
        previous_requirements.pop(key, None)
        if key == "bedrooms":
            previous_requirements.pop("min_bedrooms", None)
        if key == "max_price":
            previous_requirements.pop("min_price", None)
    new_requirements = extract_requirements(understood)
    # A chip like "Without Sheikh Zayed" names the value being removed.
    for key in to_clear:
        new_requirements.pop(key, None)
    base = classify_intent(query)
    if scheduling.get("stage") in SCHEDULING_STAGES and base != "property_search":
        # Mid-booking replies (times, phone numbers, "yes") must not be read
        # as new search filters; only an explicit new search leaves the flow.
        new_requirements = {}
    if compare_with or pasted:
        # Prices and places in a pasted line or a comparison describe listings we already showed.
        new_requirements = {}
    if changes_topic(previous_requirements, new_requirements, query):
        previous_requirements = {}  # a different kind of property somewhere else: start the search over
    requirements = merge_requirements(previous_requirements, new_requirements)

    signals = extract_qualification(understood)
    contact = {key: signals.pop(key) for key in ("name", "phone", "email") if key in signals}
    qualification = dict(state.get("qualification") or {})
    if fresh_search:
        # The profile keeps who the customer is; what they were searching for goes with the search.
        for key in ("budget", "preferred_location", "property_type"):
            qualification.pop(key, None)
    if "max_price" in typed_clear:
        qualification.pop("budget", None)
    if "district" in typed_clear:
        qualification.pop("preferred_location", None)
    qualification = {**qualification, **signals}
    # Mirror search requirements into the lead profile used by sales/CRM.
    if requirements.get("district"):
        qualification["preferred_location"] = requirements["district"]
    if requirements.get("property_type"):
        qualification["property_type"] = requirements["property_type"]
    if requirements.get("max_price") and "budget" not in qualification:
        qualification["budget"] = requirements["max_price"]
    if requirements.get("transaction_type") == "rent" and "purpose" not in qualification:
        qualification["purpose"] = "rent"
    if requirements.get("delivery") and "delivery_preference" not in qualification:
        qualification["delivery_preference"] = requirements["delivery"]

    customer = merge_contact(state.get("customer") or {}, contact, state)

    referenced = None
    if not picking_slot and not compare_with:
        referenced = resolve_property_reference(query, candidates) or (pasted[0] if pasted else None)
    selected = referenced or (None if fresh_search else state.get("selected_property"))

    if state.get("clear_requirements"):
        intent = "property_search"
    elif fresh_search and not new_requirements:
        # "Start over" with no new details: a short fixed prompt, not a questionnaire.
        intent = "search_reset"
    elif compare_with or (pasted and base != "human_handoff"):
        # Comparing, or a listing line pasted back: answer about those listings, not a new search.
        intent = "property_information"
    elif base == "human_handoff":
        intent = "human_handoff"
    elif base in {"cancel_visit", "booking_status"} and not new_requirements:
        intent = base
    elif (state.get("handoff") or {}).get("status") == "awaiting_contact" and contact.get("phone"):
        intent = "human_handoff"
    elif scheduling.get("stage") in SCHEDULING_STAGES and not new_requirements:
        # Mid-booking replies ("Saturday", "4pm", "yes") belong to scheduling.
        intent = "schedule_visit"
    elif typed_clear and requirements:
        # "...at any price": search again without the condition that was just lifted.
        intent = "property_search"
    elif base in {"property_search", "conversational", "greeting"} and referenced and not new_requirements:
        intent = "property_information"
    elif base in {"conversational", "greeting", "lead_qualification"} and new_requirements:
        intent = "property_search"
    elif base in {"investment", "sales"} and {"property_type", "district"} & new_requirements.keys():
        # "apartment in New Cairo for investment" is a search; the purpose
        # is already captured in the qualification profile above.
        intent = "property_search"
    elif base == "conversational" and (signals or contact):
        intent = "lead_qualification"
    elif base == "property_search" and not requirements and not candidates:
        # "I'm looking for a property" — collect requirements first.
        intent = "lead_qualification"
    else:
        intent = base

    update = {
        "intent": intent,
        "language": language,
        "requirements": requirements,
        "clear_requirements": [],
        "qualification": qualification,
        "customer": customer,
        "selected_property": selected,
        "compare_ids": [str(p.get("property_id")) for p in compare_with],
    }
    if fresh_search:
        # Same fields ConversationService.reset_search() clears; the customer's profile stays.
        update["candidates"] = []
        update["scheduling"] = {}
    return update


INTENT_TO_AGENT = {
    "search_reset": "conversational",
    "cancel_visit": "scheduling",
    "booking_status": "scheduling",
    "property_search": "property",
    "property_information": "property",
    "project_information": "property",
    "availability": "property",
    "payment_plan": "sales",
    "investment": "sales",
    "sales": "sales",
    "schedule_visit": "scheduling",
    "lead_qualification": "qualification",
    "human_handoff": "handoff",
}


def route_agent(state: ConversationState) -> str:
    return INTENT_TO_AGENT.get(state.get("intent", ""), "conversational")


# ---------------------------------------------------------------------------
# Helpers shared by agent nodes
# ---------------------------------------------------------------------------


def conversation_history(state: ConversationState, limit: int = 8) -> list[dict[str, str]]:
    """Recent user/assistant turns (excluding the current one) for grounding."""
    history: list[dict[str, str]] = []
    for message in (state.get("messages") or [])[:-1][-limit:]:
        role = {"human": "user", "ai": "assistant"}.get(getattr(message, "type", ""))
        content = getattr(message, "content", "")
        if role and isinstance(content, str) and content.strip():
            history.append({"role": role, "content": content})
    return history


def property_label(item: dict | None) -> str:
    if not item:
        return ""
    metadata = item.get("metadata") or {}
    kind = metadata.get("property_type") or metadata.get("type")
    place = metadata.get("district") or metadata.get("location") or metadata.get("location_district")
    description = " — ".join(str(part) for part in (kind, place) if part)
    return f"{description} (ID {item.get('property_id')})" if description else f"ID {item.get('property_id')}"


_TYPE_AR = {
    "Apartment": "شقة", "Villa": "فيلا", "Townhouse": "تاون هاوس", "Twin House": "توين هاوس", "Duplex": "دوبلكس",
    "Penthouse": "بنتهاوس", "Chalet": "شاليه", "Studio": "ستوديو", "Room": "غرفة", "Residential Land": "أرض سكنية",
    "Office": "مكتب", "Shop": "محل", "Land": "أرض",
}
_PURPOSE_AR = {"residence": "للسكن", "investment": "للاستثمار", "rent": "للإيجار", "resale": "لإعادة البيع"}


def describe_requirements(requirements: dict[str, Any], qualification: dict[str, Any], language: str) -> str:
    ar = language == "ar"
    parts: list[str] = []
    if requirements.get("property_type"):
        parts.append(_TYPE_AR.get(str(requirements["property_type"]), str(requirements["property_type"])) if ar else str(requirements["property_type"]))
    if requirements.get("district"):
        parts.append(("في " if ar else "in ") + str(district_label(requirements["district"], language)))
    if requirements.get("bedrooms"):
        parts.append(f"{requirements['bedrooms']} " + ("غرف" if ar else "bedrooms"))
    if requirements.get("min_bedrooms"):
        parts.append(f"{requirements['min_bedrooms']}+ " + ("غرف" if ar else "bedrooms"))
    # Only the budget the search really applies: a leftover profile value would be shown but ignored.
    budget = requirements.get("max_price")
    if budget:
        parts.append(("ميزانية حتى " if ar else "budget up to ") + (f"{budget:,.0f} جنيه" if ar else f"{budget:,.0f} EGP"))
    if qualification.get("down_payment"):
        parts.append(("مقدم " if ar else "down payment ") + (f"{qualification['down_payment']:,.0f} جنيه" if ar else f"{qualification['down_payment']:,.0f} EGP"))
    if qualification.get("monthly_installment"):
        parts.append(("قسط شهري " if ar else "monthly ") + (f"{qualification['monthly_installment']:,.0f} جنيه" if ar else f"{qualification['monthly_installment']:,.0f} EGP"))
    if qualification.get("purpose"):
        parts.append(_PURPOSE_AR.get(str(qualification["purpose"]), str(qualification["purpose"])) if ar else str(qualification["purpose"]))
    if qualification.get("timeline"):
        parts.append(str(qualification["timeline"]))
    return "، ".join(parts) if ar else ", ".join(parts)
