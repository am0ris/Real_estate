#!/usr/bin/env python3
"""
Controlled comparison test: Website vs WhatsApp RAG retrieval.
Runs the same normalized query through both channels using the shared
conversation_service.run_turn(), then compares retrieval results side by side.
No WhatsApp or website HTTP calls are made - both channels invoke the same
shared service entry point with different channel parameters and thread IDs.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service
from app.helpers.config import get_settings
from scripts.diagnostic_logger import log_retrieval_stage, log_request_received

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("channel_consistency")

QUERIES = [
    "Apartment for sale in New Cairo, 3 bedrooms, budget 20 million EGP",
    "Villa in Sheikh Zayed with garden",
    "Show me properties under 5 million EGP in 6th of October",
    "I need a 2 bedroom apartment in New Cairo",
    "What about the first property you showed me?",
]

WEBSITE_THREAD = "test:website:consistency_check"
WHATSAPP_THREAD = "test:whatsapp:consistency_check"


def extract_retrieval_info(result: dict[str, Any]) -> dict[str, Any]:
    props = result.get("properties", [])
    return {
        "property_ids": [str(p.get("property_id")) for p in props if p.get("property_id")],
        "document_ids": [str(p.get("chunk_id")) for p in props if p.get("chunk_id")],
        "scores": [float(p.get("score", 0.0)) for p in props],
        "cited_property_ids": result.get("cited_property_ids", []),
        "grounding_property_ids": result.get("grounding_property_ids", []),
        "response": result.get("response", ""),
        "intent": result.get("intent", ""),
        "agent": result.get("agent", ""),
        "error": result.get("error"),
        "language": result.get("language", ""),
    }


def run_channel_test(channel: str, thread_id: str, query: str) -> dict[str, Any]:
    log_request_received(channel, thread_id, query)
    started = time.monotonic()
    result = conversation_service.run_turn(
        thread_id,
        query,
        channel=channel,
        customer_id=thread_id,
        modality="text",
    )
    info = extract_retrieval_info(result.to_dict())
    info["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    info["channel"] = channel
    info["session_id"] = thread_id
    info["query"] = query
    log_retrieval_stage(
        channel=channel,
        session_id=thread_id,
        query=query,
        requirements=info.get("requirements", {}),
        collection_name=get_settings().QDRANT_COLLECTION,
        property_ids=info["property_ids"],
        document_ids=info["document_ids"],
        scores=info["scores"],
        filter_description="",
        rerank_applied=True,
        rerank_count=len(info["property_ids"]),
        context_length=len(info["response"]),
        llm_model=get_settings().GROQ_MODEL,
        history_length=0,
        status="degraded" if info["error"] else "ok",
        error=info["error"],
    )
    return info


def compare_results(website: dict[str, Any], whatsapp: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": website["query"],
        "same_property_ids": set(website["property_ids"]) == set(whatsapp["property_ids"]),
        "website_property_ids": website["property_ids"],
        "whatsapp_property_ids": whatsapp["property_ids"],
        "same_document_ids": set(website["document_ids"]) == set(whatsapp["document_ids"]),
        "same_cited_ids": set(website["cited_property_ids"]) == set(whatsapp["cited_property_ids"]),
        "website_cited": website["cited_property_ids"],
        "whatsapp_cited": whatsapp["cited_property_ids"],
        "same_grounding_ids": set(website["grounding_property_ids"]) == set(whatsapp["grounding_property_ids"]),
        "same_intent": website["intent"] == whatsapp["intent"],
        "website_intent": website["intent"],
        "whatsapp_intent": whatsapp["intent"],
        "same_agent": website["agent"] == whatsapp["agent"],
        "website_error": website["error"],
        "whatsapp_error": whatsapp["error"],
        "website_response_len": len(website["response"]),
        "whatsapp_response_len": len(whatsapp["response"]),
        "website_response_preview": website["response"][:200],
        "whatsapp_response_preview": whatsapp["response"][:200],
    }


def main() -> int:
    settings = get_settings()
    print(f"Collection: {settings.QDRANT_COLLECTION}")
    print(f"Embedding dimension: 768")
    print(f"LLM model: {settings.GROQ_MODEL}")
    print()

    all_comparisons: list[dict[str, Any]] = []

    for idx, query in enumerate(QUERIES, 1):
        print(f"\n{'='*70}")
        print(f"TEST {idx}: {query}")
        print(f"{'='*70}")

        website_thread = f"{WEBSITE_THREAD}:{idx}"
        whatsapp_thread = f"{WHATSAPP_THREAD}:{idx}"

        website_result = run_channel_test("website", website_thread, query)
        whatsapp_result = run_channel_test("whatsapp", whatsapp_thread, query)

        comparison = compare_results(website_result, whatsapp_result)
        all_comparisons.append(comparison)

        print(f"\n  Website -> intent={website_result['intent']}, agent={website_result['agent']}")
        print(f"  WhatsApp -> intent={whatsapp_result['intent']}, agent={whatsapp_result['agent']}")
        print(f"  Website props: {website_result['property_ids']}")
        print(f"  WhatsApp props: {whatsapp_result['property_ids']}")
        print(f"  Same properties: {comparison['same_property_ids']}")
        print(f"  Website error: {website_result['error']}")
        print(f"  WhatsApp error: {whatsapp_result['error']}")
        print(f"  Website response ({len(website_result['response'])} chars): {website_result['response'][:150]}...")
        print(f"  WhatsApp response ({len(whatsapp_result['response'])} chars): {whatsapp_result['response'][:150]}...")

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    all_same_props = all(c["same_property_ids"] for c in all_comparisons)
    all_same_cited = all(c["same_cited_ids"] for c in all_comparisons)
    all_same_intent = all(c["same_intent"] for c in all_comparisons)
    any_error = any(c["website_error"] or c["whatsapp_error"] for c in all_comparisons)

    print(f"All same property IDs: {all_same_props}")
    print(f"All same cited IDs: {all_same_cited}")
    print(f"All same intent: {all_same_intent}")
    print(f"Any errors: {any_error}")

    if all_same_props and all_same_cited and all_same_intent and not any_error:
        print("\nCONCLUSION: Both channels produce identical retrieval results.")
        return 0
    else:
        print("\nCONCLUSION: Differences detected between channels.")
        return 1


if __name__ == "__main__":
    sys.exit(main())