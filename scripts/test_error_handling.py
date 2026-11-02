#!/usr/bin/env python3
"""Error handling and recovery test."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.service import conversation_service

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("error_handling_test")


def main():
    print("=" * 60)
    print("TEST: Error Handling and Recovery")
    print("=" * 60)

    # Simulate a Qdrant failure by patching the search
    thread = "test:error:user"

    # First, verify normal operation works
    print("\n--- Normal operation ---")
    result_normal = conversation_service.run_turn(
        thread,
        "apartment New Cairo",
        channel="whatsapp",
        customer_id=thread,
        modality="text",
    )
    print(f"Response: {result_normal.response[:100]}...")
    print(f"Error: {result_normal.error}")
    assert result_normal.error is None, "Normal operation should not have errors"
    print("PASS: Normal operation works")

    # Now simulate Qdrant failure
    print("\n--- Simulating Qdrant failure ---")
    from app.database.vector_db import create_client

    original_search = None

    def failing_search(self, **kwargs):
        from qdrant_client.http.exceptions import UnexpectedResponse
        raise UnexpectedResponse.for_response(
            type("Response", (), {"status_code": 500, "content": b"Internal Server Error"})()
        )

    # Patch QdrantClient.search to fail
    from qdrant_client import QdrantClient
    original_search_method = QdrantClient.search

    def mock_search(self, **kwargs):
        raise Exception("Simulated Qdrant connection failure")

    QdrantClient.search = mock_search

    try:
        result_error = conversation_service.run_turn(
            thread,
            "apartment New Cairo",
            channel="whatsapp",
            customer_id=thread,
            modality="text",
        )
        print(f"Response: {result_error.response[:100]}...")
        print(f"Error: {result_error.error}")
        # The workflow catches the exception and returns a fallback
        assert result_error.error is not None, "Error should be set when Qdrant fails"
        assert "search_unavailable" in result_error.response.lower() or "can't reach" in result_error.response.lower(), \
            "User should get a safe fallback message"
        print("PASS: Qdrant failure returns safe fallback message with error set")
    finally:
        QdrantClient.search = original_search_method

    # Recovery: Qdrant is available again
    print("\n--- Recovery after Qdrant restoration ---")
    result_recovered = conversation_service.run_turn(
        thread,
        "apartment New Cairo",
        channel="whatsapp",
        customer_id=thread,
        modality="text",
    )
    print(f"Response: {result_recovered.response[:100]}...")
    print(f"Error: {result_recovered.error}")
    assert result_recovered.error is None, "Recovery should clear errors"
    print("PASS: Recovery works - errors cleared after Qdrant restored")

    print("\nOverall error handling: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())