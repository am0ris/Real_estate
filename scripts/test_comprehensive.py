#!/usr/bin/env python3
"""Comprehensive channel consistency test including multi-turn conversation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service


def test_property_search():
    print("=" * 60)
    print("TEST 1: Property search with filters")
    print("=" * 60)
    query = "Apartment for sale in New Cairo, 3 bedrooms, budget 20 million EGP"
    result_w = conversation_service.run_turn(
        "test:web:1", query, channel="website",
        customer_id="test:web:1", modality="text",
    )
    result_wa = conversation_service.run_turn(
        "test:wa:1", query, channel="whatsapp",
        customer_id="test:wa:1", modality="text",
    )
    w_props = [p.get("property_id") for p in result_w.properties]
    wa_props = [p.get("property_id") for p in result_wa.properties]
    print(f"Website props: {w_props}")
    print(f"WhatsApp props: {wa_props}")
    print(f"Same: {w_props == wa_props}")
    print(f"Website error: {result_w.error}")
    print(f"WhatsApp error: {result_wa.error}")
    print(f"Website response: {result_w.response[:120]}...")
    print(f"WhatsApp response: {result_wa.response[:120]}...")
    return w_props == wa_props and result_w.error == result_wa.error


def test_conversation_history():
    print()
    print("=" * 60)
    print("TEST 2: Follow-up question (conversation history)")
    print("=" * 60)
    result_w = conversation_service.run_turn(
        "test:web:1", "What about the first property? Tell me more details.",
        channel="website", customer_id="test:web:1", modality="text",
    )
    result_wa = conversation_service.run_turn(
        "test:wa:1", "What about the first property? Tell me more details.",
        channel="whatsapp", customer_id="test:wa:1", modality="text",
    )
    w_props = [p.get("property_id") for p in result_w.properties]
    wa_props = [p.get("property_id") for p in result_wa.properties]
    print(f"Website intent: {result_w.intent}, agent: {result_w.agent}")
    print(f"WhatsApp intent: {result_wa.intent}, agent: {result_wa.agent}")
    print(f"Website props: {w_props}")
    print(f"WhatsApp props: {wa_props}")
    print(f"Website error: {result_w.error}")
    print(f"WhatsApp error: {result_wa.error}")
    print(f"Website response: {result_w.response[:150]}...")
    print(f"WhatsApp response: {result_wa.response[:150]}...")
    return result_w.intent == result_wa.intent and result_w.error == result_wa.error


def test_budget_search():
    print()
    print("=" * 60)
    print("TEST 3: Budget range search")
    print("=" * 60)
    query = "Show me properties under 5 million EGP in 6th of October"
    result_w = conversation_service.run_turn(
        "test:web:3", query, channel="website",
        customer_id="test:web:3", modality="text",
    )
    result_wa = conversation_service.run_turn(
        "test:wa:3", query, channel="whatsapp",
        customer_id="test:wa:3", modality="text",
    )
    w_props = [p.get("property_id") for p in result_w.properties]
    wa_props = [p.get("property_id") for p in result_wa.properties]
    print(f"Website props: {w_props}")
    print(f"WhatsApp props: {wa_props}")
    print(f"Same: {w_props == wa_props}")
    print(f"Website error: {result_w.error}")
    print(f"WhatsApp error: {result_wa.error}")
    print(f"Website response: {result_w.response[:150]}...")
    print(f"WhatsApp response: {result_wa.response[:150]}...")
    return w_props == wa_props and result_w.error == result_wa.error


def test_multi_turn():
    print()
    print("=" * 60)
    print("TEST 4: Multi-turn conversation (website)")
    print("=" * 60)
    # Turn 1
    r1 = conversation_service.run_turn(
        "test:web:4", "I'm looking for a villa in Sheikh Zayed",
        channel="website", customer_id="test:web:4", modality="text",
    )
    # Turn 2
    r2 = conversation_service.run_turn(
        "test:web:4", "What about under 8 million EGP?",
        channel="website", customer_id="test:web:4", modality="text",
    )
    # Turn 3
    r3 = conversation_service.run_turn(
        "test:web:4", "Show me the first one",
        channel="website", customer_id="test:web:4", modality="text",
    )
    print(f"Turn 1 props: {[p.get('property_id') for p in r1.properties]}")
    print(f"Turn 2 props: {[p.get('property_id') for p in r2.properties]}")
    print(f"Turn 3 props: {[p.get('property_id') for p in r3.properties]}")
    print(f"Turn 3 intent: {r3.intent}, agent: {r3.agent}")
    print(f"Turn 3 error: {r3.error}")
    print(f"Turn 3 response: {r3.response[:150]}...")
    return r3.error is None


def main():
    results = []
    results.append(("Property search with filters", test_property_search()))
    results.append(("Conversation history", test_conversation_history()))
    results.append(("Budget range search", test_budget_search()))
    results.append(("Multi-turn conversation", test_multi_turn()))

    print()
    print("=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}")
    print()
    if all_pass:
        print("All tests passed - channels are consistent.")
    else:
        print("Some tests failed - inconsistencies detected.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())