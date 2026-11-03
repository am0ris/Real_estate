#!/usr/bin/env python3
"""
Detailed stage-by-stage trace of "فلل" through Website and WhatsApp channels.
Tests both fresh sessions and sessions with stale accumulated requirements.
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
from app.agents.understanding import extract_requirements, classify_intent
from app.agents.state import FILTER_KEYS

QUERY = "فلل"


def trace_stage(stage: str, channel: str, session: str, **kwargs):
    parts = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    print(f"  [{stage}] channel={channel} session={session[-8:]} | {parts}")


def run_traced_turn(channel: str, thread_id: str, query: str) -> dict:
    result = conversation_service.run_turn(
        thread_id,
        query,
        channel=channel,
        customer_id=thread_id,
        modality="text",
    )
    d = result.to_dict()
    props = d.get("properties", [])
    return {
        "intent": d.get("intent"),
        "agent": d.get("agent"),
        "error": d.get("error"),
        "requirements": d.get("requirements", {}),
        "property_ids": [str(p.get("property_id")) for p in props if p.get("property_id")],
        "cited_ids": d.get("cited_property_ids", []),
        "response": d.get("response", ""),
    }


def direct_qdrant_trace(requirements: dict) -> dict:
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
        raw_count = len(points)
        prop_ids_raw = []
        for p in points:
            payload = getattr(p, "payload", None) or {}
            pid = payload.get("property_id")
            if pid:
                prop_ids_raw.append(str(pid))
        from app.helpers.config import get_settings as gs
        after_demo = raw_count if gs().SHOW_DEMO_LISTINGS else raw_count
        from app.rag.retrieval.reranker import rerank
        from app.rag.retrieval.hybrid_search import SearchResult
        results = []
        for p in points:
            payload = getattr(p, "payload", None) or {}
            flattened = {}
            nested = payload.get("metadata")
            if isinstance(nested, dict):
                flattened = dict(nested)
            for key, value in payload.items():
                if key != "metadata":
                    flattened[key] = value
            results.append(SearchResult(
                property_id=str(payload.get("property_id", "")) if payload.get("property_id") else None,
                chunk_id=str(payload.get("chunk_id", "")) if payload.get("chunk_id") else None,
                text=str(payload.get("text", "")),
                metadata=flattened,
                combined_score=float(getattr(p, "score", 0.0) or 0.0),
            ))
        reranked = rerank(QUERY, results, top_k=5)
        rerank_ids = [str(r.property_id) for r in reranked if r.property_id]
        return {
            "raw_count": raw_count,
            "after_demo": after_demo,
            "after_rerank": len(reranked),
            "rerank_ids": rerank_ids,
            "filter_describe": filt.describe() if filt else "(no filter)",
            "filter": qf,
        }
    finally:
        client.close()


def main() -> int:
    settings = get_settings()
    print(f"Collection: {settings.QDRANT_COLLECTION}")
    print(f"Query: {QUERY!r}")
    print(f"Extracted requirements: {extract_requirements(QUERY)}")
    print(f"Classified intent: {classify_intent(QUERY)}")
    print(f"FILTER_KEYS: {FILTER_KEYS}")
    print()

    print("=" * 70)
    print("TEST 1: Fresh sessions (no prior conversation state)")
    print("=" * 70)

    print("\n--- Direct Qdrant search (property_type=Villa) ---")
    direct = direct_qdrant_trace({"property_type": "Villa"})
    trace_stage("qdrant_raw", "direct", "n/a", count=direct["raw_count"])
    trace_stage("qdrant_after_demo", "direct", "n/a", count=direct["after_demo"])
    trace_stage("rerank_output", "direct", "n/a", count=direct["after_rerank"], ids=direct["rerank_ids"][:5])

    print("\n--- Website: Fresh session ---")
    web = run_traced_turn("website", "trace:web:fresh", QUERY)
    trace_stage("intent_classify", "website", "fresh", intent=web["intent"], agent=web["agent"])
    trace_stage("requirements", "website", "fresh", req=web["requirements"])
    trace_stage("qdrant_results", "website", "fresh", count=len(web["property_ids"]), ids=web["property_ids"][:5])
    trace_stage("llm_generation", "website", "fresh", response_len=len(web["response"]), error=web["error"])

    print("\n--- WhatsApp: Fresh session ---")
    wa = run_traced_turn("whatsapp", "trace:wa:fresh", QUERY)
    trace_stage("intent_classify", "whatsapp", "fresh", intent=wa["intent"], agent=wa["agent"])
    trace_stage("requirements", "whatsapp", "fresh", req=wa["requirements"])
    trace_stage("qdrant_results", "whatsapp", "fresh", count=len(wa["property_ids"]), ids=wa["property_ids"][:5])
    trace_stage("llm_generation", "whatsapp", "fresh", response_len=len(wa["response"]), error=wa["error"])

    print("\n--- Fresh session comparison ---")
    print(f"  Same property IDs: {web['property_ids'] == wa['property_ids']}")
    print(f"  Same intent: {web['intent'] == wa['intent']}")
    print(f"  Same error: {web['error'] == wa['error']}")
    print(f"  Same requirements: {web['requirements'] == wa['requirements']}")

    print("\n" + "=" * 70)
    print("TEST 2: Stale requirements (previous restrictive search persists)")
    print("=" * 70)

    restrictive_query = "apartment for sale in New Cairo, 2 bedrooms, budget 5 million EGP"

    print(f"\n--- Pre-search: {restrictive_query} ---")
    web_pre = run_traced_turn("website", "trace:web:stale", restrictive_query)
    wa_pre = run_traced_turn("whatsapp", "trace:wa:stale", restrictive_query)
    trace_stage("pre_requirements", "website", "stale", req=web_pre["requirements"])
    trace_stage("pre_requirements", "whatsapp", "stale", req=wa_pre["requirements"])
    trace_stage("pre_results", "website", "stale", count=len(web_pre["property_ids"]))
    trace_stage("pre_results", "whatsapp", "stale", count=len(wa_pre["property_ids"]))

    print("\n--- Direct Qdrant search with stale requirements ---")
    direct_stale = direct_qdrant_trace(web_pre["requirements"])
    trace_stage("qdrant_raw", "direct", "stale", count=direct_stale["raw_count"])
    trace_stage("qdrant_after_demo", "direct", "stale", count=direct_stale["after_demo"])
    trace_stage("rerank_output", "direct", "stale", count=direct_stale["after_rerank"], ids=direct_stale["rerank_ids"][:5])

    print(f"\n--- Follow-up: {QUERY} (on stale session) ---")
    web_stale = run_traced_turn("website", "trace:web:stale", QUERY)
    wa_stale = run_traced_turn("whatsapp", "trace:wa:stale", QUERY)
    trace_stage("followup_requirements", "website", "stale", req=web_stale["requirements"])
    trace_stage("followup_requirements", "whatsapp", "stale", req=wa_stale["requirements"])
    trace_stage("followup_results", "website", "stale", count=len(web_stale["property_ids"]), error=web_stale["error"])
    trace_stage("followup_results", "whatsapp", "stale", count=len(wa_stale["property_ids"]), error=wa_stale["error"])
    trace_stage("llm_generation", "website", "stale", response_len=len(web_stale["response"]))
    trace_stage("llm_generation", "whatsapp", "stale", response_len=len(wa_stale["response"]))

    print("\n--- Stale session comparison ---")
    print(f"  Same property IDs: {web_stale['property_ids'] == wa_stale['property_ids']}")
    print(f"  Same requirements: {web_stale['requirements'] == wa_stale['requirements']}")
    print(f"  Same error: {web_stale['error'] == wa_stale['error']}")
    print(f"  Website response: {web_stale['response'][:120]}...")
    print(f"  WhatsApp response: {wa_stale['response'][:120]}...")

    print("\n" + "=" * 70)
    print("TEST 3: Direct Qdrant filter comparison")
    print("=" * 70)

    print(f"\nFresh filter (Villa only):")
    print(f"  Filter: {direct['filter_describe']}")
    print(f"  Raw results: {direct['raw_count']}")
    print(f"  After rerank: {direct['after_rerank']}")

    print(f"\nStale filter (Villa + New Cairo + 2BR + sale + max 5M):")
    print(f"  Filter: {direct_stale['filter_describe']}")
    print(f"  Raw results: {direct_stale['raw_count']}")
    print(f"  After rerank: {direct_stale['after_rerank']}")

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    fresh_consistent = (
        web["property_ids"] == wa["property_ids"]
        and web["intent"] == wa["intent"]
        and web["error"] == wa["error"]
    )
    stale_consistent = (
        web_stale["property_ids"] == wa_stale["property_ids"]
        and web_stale["requirements"] == wa_stale["requirements"]
        and web_stale["error"] == wa_stale["error"]
    )

    print(f"  Fresh sessions consistent: {fresh_consistent}")
    print(f"  Stale sessions consistent: {stale_consistent}")
    print(f"  Direct Qdrant fresh results: {direct['raw_count']} raw → {direct['after_rerank']} after rerank")
    print(f"  Direct Qdrant stale results: {direct_stale['raw_count']} raw → {direct_stale['after_rerank']} after rerank")

    if fresh_consistent and stale_consistent:
        print("\n  CONCLUSION: Both channels are IDENTICAL. No code divergence.")
        print("  The production difference is due to SESSION STATE, not code.")
        print("  WhatsApp accumulates requirements across turns with no clear mechanism.")
    else:
        print("\n  CONCLUSION: CODE DIVERGENCE DETECTED between channels.")

    return 0 if (fresh_consistent and stale_consistent) else 1


if __name__ == "__main__":
    sys.exit(main())