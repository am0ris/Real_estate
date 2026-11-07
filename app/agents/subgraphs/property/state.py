from __future__ import annotations

import logging

from app.agents.listing_text import dedupe_listings, format_property_list
from app.agents.messages import say
from app.agents.understanding import extract_requirements
from app.agents.state import (
    FILTER_KEYS,
    ConversationState,
    conversation_history,
    describe_requirements,
)


logger = logging.getLogger(__name__)

# Intents answered from the already-selected listing rather than a new search.
DETAIL_INTENTS = {"property_information", "availability", "project_information"}


class PropertyState(
    ConversationState,
    total=False,
):
    # --------------------------------------------------------
    # Query
    # --------------------------------------------------------

    rewritten_query: str

    # --------------------------------------------------------
    # Structured requirements
    # --------------------------------------------------------

    requirements: dict

    # --------------------------------------------------------
    # Retrieval
    # --------------------------------------------------------

    candidates: list[dict]

    # --------------------------------------------------------
    # Selection
    # --------------------------------------------------------

    selected_property: dict | None

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    response: str


def search_filters(requirements: dict) -> dict:
    """Hard metadata filters understood by ``build_metadata_filter``."""
    return {key: requirements[key] for key in FILTER_KEYS if requirements.get(key) not in (None, "")}


def rewrite_query(state: PropertyState) -> str:
    """Carry earlier requirements into the retrieval query text."""
    query = state.get("query", "").strip()
    carried = describe_requirements(state.get("requirements") or {}, {}, "en")
    return f"{query} — {carried}" if carried and carried.lower() not in query.lower() else query


def run_property_workflow(state: PropertyState) -> dict:
    """Execute the same RAG workflow used by HTTP and channel adapters."""
    from app.workflows.rag import answer_about_properties, answer_query

    language = state.get("language", "en")
    requirements = state.get("requirements") or {}
    history = conversation_history(state)
    selected = state.get("selected_property")

    # The catalogue only holds properties for sale. Say so instead of showing sale listings to
    # someone who asked to rent. Only the current message counts, never a remembered one.
    if state.get("intent") not in DETAIL_INTENTS and (
        extract_requirements(state.get("query", "")).get("transaction_type") == "rent"
    ):
        return {"response": say(language, "rent_unavailable"), "active_agent": "property"}

    # Channels without property cards (WhatsApp) get the listings as text after a short answer.
    text_only = state.get("channel") == "whatsapp"
    extra = {"channel": "whatsapp"} if text_only else {}

    try:
        compare_ids = {str(property_id) for property_id in (state.get("compare_ids") or [])}
        to_compare = [c for c in (state.get("candidates") or []) if str(c.get("property_id")) in compare_ids]
        if len(to_compare) >= 2:
            # Compare exactly the listings the customer was shown, never a fresh search.
            result = answer_about_properties(
                state.get("query", ""),
                to_compare,
                requirements,
                conversation_history=history,
                **extra,
            )
            return {
                "response": result["answer"],
                "turn_properties": result["properties"],
                "cited_property_ids": result["cited_property_ids"],
                "grounding_property_ids": result["grounding_property_ids"],
                "active_agent": "property",
            }

        if state.get("intent") in DETAIL_INTENTS and selected:
            result = answer_about_properties(
                state.get("query", ""),
                [selected],
                requirements,
                conversation_history=history,
                **extra,
            )
            return {
                "response": result["answer"],
                "turn_properties": result["properties"],
                "cited_property_ids": result["cited_property_ids"],
                "grounding_property_ids": result["grounding_property_ids"],
                "active_agent": "property",
            }

        rewritten = rewrite_query(state)
        result = answer_query(
            rewritten,
            search_filters(requirements),
            conversation_history=history,
            **extra,
        )
    except Exception as error:  # provider, vector DB or timeout failures
        logger.exception("Property workflow failed")
        return {
            "response": say(language, "search_unavailable"),
            "error": f"property_search_unavailable: {type(error).__name__}",
            "active_agent": "property",
        }

    properties = dedupe_listings(result["properties"]) if text_only else result["properties"]
    answer = result["answer"]
    update = {
        "rewritten_query": rewritten,
        "turn_properties": properties,
        "cited_property_ids": result["cited_property_ids"],
        "grounding_property_ids": result["grounding_property_ids"],
        "active_agent": "property",
    }
    if properties:
        if text_only:
            conditions = describe_requirements(requirements, {}, language)
            listing = format_property_list(properties, language, conditions=conditions)
            answer = f"{answer}\n\n{listing}".strip()
        # Only a new, non-empty result list replaces the listings the customer was shown.
        update["candidates"] = properties
        update["selected_property"] = None
    else:
        # An empty search must not wipe the list we showed ("compare them", "book the first").
        # Makes an over-narrow filter visible in the logs (filters hold no personal data).
        logger.info(
            "Property search returned no listings | channel=%s filters=%s",
            state.get("channel"),
            search_filters(requirements),
        )
        summary = describe_requirements(requirements, {}, language)
        if summary:
            answer = f"{answer}\n\n{say(language, 'search_context', summary=summary)}"
    update["response"] = answer
    return update
