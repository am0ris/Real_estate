#!/usr/bin/env python3
"""WhatsApp session isolation test: two independent users."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service


def main():
    # User A (WhatsApp phone +201000000001)
    user_a_thread = "whatsapp:+201000000001"
    # User B (WhatsApp phone +201000000002)
    user_b_thread = "whatsapp:+201000000002"

    print("=" * 60)
    print("TEST: WhatsApp Session Isolation - Two Users")
    print("=" * 60)

    # User A searches for properties
    print("\n--- User A searches for apartments in New Cairo ---")
    a1 = conversation_service.run_turn(
        user_a_thread,
        "I want an apartment in New Cairo, 2 bedrooms",
        channel="whatsapp",
        customer_id=user_a_thread,
        modality="text",
    )
    a_props_1 = [p.get("property_id") for p in a1.properties]
    print(f"User A props: {a_props_1}")
    print(f"User A error: {a1.error}")

    # User B searches for different properties
    print("\n--- User B searches for villas in Sheikh Zayed ---")
    b1 = conversation_service.run_turn(
        user_b_thread,
        "I want a villa in Sheikh Zayed, 4 bedrooms",
        channel="whatsapp",
        customer_id=user_b_thread,
        modality="text",
    )
    b_props_1 = [p.get("property_id") for p in b1.properties]
    print(f"User B props: {b_props_1}")
    print(f"User B error: {b1.error}")

    # User A asks a follow-up - should NOT see User B's properties
    print("\n--- User A follow-up: 'Tell me more about the first one' ---")
    a2 = conversation_service.run_turn(
        user_a_thread,
        "Tell me more about the first one",
        channel="whatsapp",
        customer_id=user_a_thread,
        modality="text",
    )
    a_props_2 = [p.get("property_id") for p in a2.properties]
    print(f"User A follow-up props: {a_props_2}")
    print(f"User A follow-up intent: {a2.intent}")
    print(f"User A follow-up response: {a2.response[:150]}...")

    # User B asks a follow-up - should NOT see User A's properties
    print("\n--- User B follow-up: 'What about the price?' ---")
    b2 = conversation_service.run_turn(
        user_b_thread,
        "What about the price?",
        channel="whatsapp",
        customer_id=user_b_thread,
        modality="text",
    )
    b_props_2 = [p.get("property_id") for p in b2.properties]
    print(f"User B follow-up props: {b_props_2}")
    print(f"User B follow-up intent: {b2.intent}")
    print(f"User B follow-up response: {b2.response[:150]}...")

    # Verify isolation
    print("\n--- Verification ---")
    isolation_ok = True

    # User A's properties should not include User B's properties
    if set(a_props_1) & set(b_props_1):
        print("FAIL: User A and User B share properties in initial search")
        isolation_ok = False
    else:
        print("PASS: Initial search results are isolated between users")

    # User A's follow-up should only reference User A's properties
    if set(a_props_2) - set(a_props_1):
        print("FAIL: User A's follow-up references properties not from User A's search")
        isolation_ok = False
    else:
        print("PASS: User A's follow-up only references User A's properties")

    # User B's follow-up should only reference User B's properties
    if set(b_props_2) - set(b_props_1):
        print("FAIL: User B's follow-up references properties not from User B's search")
        isolation_ok = False
    else:
        print("PASS: User B's follow-up only references User B's properties")

    # Errors should be None for both
    if a1.error or b1.error or a2.error or b2.error:
        print(f"FAIL: Errors detected - A1: {a1.error}, B1: {b1.error}, A2: {a2.error}, B2: {b2.error}")
        isolation_ok = False
    else:
        print("PASS: No errors for either user")

    print(f"\nOverall isolation: {'PASS' if isolation_ok else 'FAIL'}")
    return 0 if isolation_ok else 1


if __name__ == "__main__":
    sys.exit(main())