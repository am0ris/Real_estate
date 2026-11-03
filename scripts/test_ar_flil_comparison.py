#!/usr/bin/env python3
"""
Controlled comparison: Arabic query "فلل" (villas) through Website vs WhatsApp.
Runs the same query through both channels using the shared conversation_service,
with both fresh sessions and sessions with stale requirements.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service
from app.database.vector_db import create_client
from app.helpers.config import get_settings
from app.rag.retrieval.hybrid_search import embed_query, _to_qdrant_filter
from app.rag.retrieval.metadata_filter import build_metadata_filter
from app.agents.understanding import extract_requirements, classify_intent, normalize_text

QUERY = "فلل"


def extract_retrieval_info(result: dict) -> dict:
    props = result.get("properties", [])
    return {
        "property_ids": [str(p.get("property_id")) for p in props if p.get("property_id")],
        "cited_property_ids": result.get("cited_property_ids", []),
        "grounding_property_ids": result.get("grounding_property_ids", []),
        "response": result.get("response", ""),
        "intent": result.get("intent", ""),
        "agent": result.get("agent", ""),
        "error": result.get("error"),
        "requirements": result.get("requirements", {}),
        "language": result.get("language", ""),
    }


def run_channel_test(channel: str, thread_id: str, query: str) -> dict:
    result = conversation_service.run_turn(
        thread_id,
        query,
        channel=channel,
        customer_id=thread_id,
        modality="text",
    )
    return extract_retrieval_info(result.to_dict())


def direct_qdrant_search(requirements: dict) -> dict:
    settings = get_settings()
    filt = build_metadata_filter(requirements or {})
    qf = _to_qdrant_filter(filt)
    client = create_client()
    try:
        vec = embed_query(QUERY)
        search_kwargs = {
            "collection_name": settings.QDRANT_COLLECTION,
            "limit": 20,
            "query_vector": vec,
        }
        if qf is not None:
            search_kwargs["query_filter"] = qf
        points = client.search(**search_kwargs)
        prop_ids = []
        for p in points:
            payload = getattr(p, "payload", None) or {}
            pid = payload.get("property_id")
            if pid:
                prop_ids.append(str(pid))
        return {
            "raw_count": len(points),
            "property_ids": prop_ids,
            "filter": qf,
            "filter_describe": filt.describe() if filt else "(no filter)",
        }
    finally:
        client.close()


def main() -> int:
    settings = get_settings()
    print(f"Collection: {settings.QDRANT_COLLECTION}")
    print(f"Query: {QUERY!r}")
    print(f"Normalized: {normalize_text(QUERY)!r}")
    print(f"Extracted requirements: {extract_requirements(QUERY)}")
    print(f"Classified intent: {classify_intent(QUERY)}")
    print()

    print("=" * 70)
    print("DIRECT QDRANT SEARCH: property_type=Villa (no stale state)")
    print("=" * 70)
    direct = direct_qdrant_search({"property_type": "Villa"})
    print(f"Filter: {direct['filter_describe']}")
    print(f"Raw Qdrant results: {direct['raw_count']}")
    print(f"Property IDs: {direct['property_ids'][:10]}...")
    print()

    print("=" * 70)
    print("WEBSITE: Fresh session")
    print("=" * 70)
    web_fresh = run_channel_test("website", "test:web:flil:fresh", QUERY)
    print(f"Intent: {web_fresh['intent']}, Agent: {web_fresh['agent']}")
    print(f"Error: {web_fresh['error']}")
    print(f"Requirements: {web_fresh['requirements']}")
    print(f"Property IDs: {web_fresh['property_ids']}")
    print(f"Cited IDs: {web_fresh['cited_property_ids']}")
    print(f"Response: {web_fresh['response'][:200]}...")
    print()

    print("=" * 70)
    print("WHATSAPP: Fresh session")
    print("=" * 70)
    wa_fresh = run_channel_test("whatsapp", "test:wa:flil:fresh", QUERY)
    print(f"Intent: {wa_fresh['intent']}, Agent: {wa_fresh['agent']}")
    print(f"Error: {wa_fresh['error']}")
    print(f"Requirements: {wa_fresh['requirements']}")
    print(f"Property IDs: {wa_fresh['property_ids']}")
    print(f"Cited IDs: {wa_fresh['cited_property_ids']}")
    print(f"Response: {wa_fresh['response'][:200]}...")
    print()

    print("=" * 70)
    print("COMPARISON: Fresh sessions")
    print("=" * 70)
    print(f"Same property IDs: {web_fresh['property_ids'] == wa_fresh['property_ids']}")
    print(f"Same intent: {web_fresh['intent'] == wa_fresh['intent']}")
    print(f"Same error: {web_fresh['error'] == wa_fresh['error']}")
    print(f"Website error: {web_fresh['error']}")
    print(f"WhatsApp error: {wa_fresh['error']}")
    print()

    print("=" * 70)
    print("TEST: Stale requirements (previous restrictive search)")
    print("=" * 70)
    restrictive_query = "apartment for sale in New Cairo, 2 bedrooms, budget 5 million EGP"
    print(f"Pre-search query: {restrictive_query}")

    web_stale_thread = "test:web:flil:stale"
    wa_stale_thread = "test:wa:flil:stale"

    web_pre = run_channel_test("website", web_stale_thread, restrictive_query)
    wa_pre = run_channel_test("whatsapp", wa_stale_thread, restrictive_query)
    print(f"Website pre-search props: {web_pre['property_ids']}")
    print(f"WhatsApp pre-search props: {wa_pre['property_ids']}")
    print(f"Website pre-search requirements: {web_pre['requirements']}")
    print(f"WhatsApp pre-search requirements: {wa_pre['requirements']}")

    print(f"\nFollow-up query: {QUERY}")
    web_stale = run_channel_test("website", web_stale_thread, QUERY)
    wa_stale = run_channel_test("whatsapp", wa_stale_thread, QUERY)
    print(f"Website stale props: {web_stale['property_ids']}")
    print(f"WhatsApp stale props: {wa_stale['property_ids']}")
    print(f"Website stale requirements: {web_stale['requirements']}")
    print(f"WhatsApp stale requirements: {wa_stale['requirements']}")
    print(f"Website stale error: {web_stale['error']}")
    print(f"WhatsApp stale error: {wa_stale['error']}")
    print(f"Website stale response: {web_stale['response'][:150]}...")
    print(f"WhatsApp stale response: {wa_stale['response'][:150]}...")
    print()

    print("=" * 70)
    print("COMPARISON: Stale sessions")
    print("=" * 70)
    print(f"Same property IDs: {web_stale['property_ids'] == wa_stale['property_ids']}")
    print(f"Same requirements: {web_stale['requirements'] == wa_stale['requirements']}")
    print(f"Same error: {web_stale['error'] == wa_stale['error']}")
    print()

    all_ok = (
        web_fresh["property_ids"] == wa_fresh["property_ids"]
        and web_stale["property_ids"] == wa_stale["property_ids"]
        and web_fresh["error"] == wa_fresh["error"]
        and web_stale["error"] == wa_stale["error"]
    )
    print("=" * 70)
    if all_ok:
        print("VERDICT: Both channels are consistent with the same state.")
    else:
        print("VERDICT: DIVERGENCE DETECTED between channels.")
    print("=" * 70)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())