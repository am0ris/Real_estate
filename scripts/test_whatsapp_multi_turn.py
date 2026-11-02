#!/usr/bin/env python3
"""Multi-turn WhatsApp conversation test: 5 consecutive messages."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service


def main():
    thread = "whatsapp:+201000000100"
    print("=" * 60)
    print("TEST: Multi-Turn WhatsApp Conversation (5 turns)")
    print("=" * 60)

    turns = [
        ("Turn 1: Initial search", "I'm looking for an apartment in New Cairo, 2 bedrooms, budget 5 million EGP"),
        ("Turn 2: Budget change", "Actually, my budget is up to 8 million EGP"),
        ("Turn 3: Location change", "I changed my mind, I prefer Sheikh Zayed instead of New Cairo"),
        ("Turn 4: Reference resolution", "Show me the first property you found"),
        ("Turn 5: More details", "What are the payment plan options for that property?"),
    ]

    all_pass = True
    prev_props = None

    for turn_name, query in turns:
        print(f"\n--- {turn_name} ---")
        print(f"User: {query}")

        result = conversation_service.run_turn(
            thread,
            query,
            channel="whatsapp",
            customer_id=thread,
            modality="text",
        )

        props = [p.get("property_id") for p in result.properties]
        print(f"Intent: {result.intent}, Agent: {result.agent}")
        print(f"Props: {props}")
        print(f"Error: {result.error}")
        print(f"Response: {result.response[:200]}...")

        if result.error:
            print(f"FAIL: Error in {turn_name}: {result.error}")
            all_pass = False

        # Turn 4 should reference a property from Turn 3 results
        if turn_name.startswith("Turn 4"):
            if not props:
                print("FAIL: Turn 4 ('the first property') returned no properties")
                all_pass = False
            elif prev_props and props[0] != prev_props[0]:
                print(f"FAIL: Turn 4 referenced {props[0]} but expected {prev_props[0]}")
                all_pass = False
            else:
                print("PASS: Turn 4 correctly referenced the first property")

        prev_props = props

    print(f"\nOverall multi-turn: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())