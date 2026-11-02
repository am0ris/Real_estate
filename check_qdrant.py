#!/usr/bin/env python3
"""Qdrant health-check script for the Real Estate Assistant.

Verifies:
1. Qdrant connection is available
2. Expected collection exists
3. Collection contains points
4. All required payload indexes exist
5. Any required indexes are missing
6. Collection configuration is compatible with the retrieval implementation

Produces clear PASS/FAIL results and a diagnostic report.

Uses the same centralized index definitions as the application
(app.database.vector_db.PAYLOAD_INDEXES).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.database.vector_db import (
    PAYLOAD_INDEXES,
    DEFAULT_COLLECTION,
    create_client,
)
from app.helpers.config import get_settings
from app.rag.embeddings import embedding_dimension

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger("check_qdrant")

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_checks: list[dict[str, Any]] = []


def _record(name: str, ok: bool, detail: str = "", error: str = "") -> None:
    _checks.append(
        {
            "name": name,
            "ok": ok,
            "detail": detail,
            "error": error,
        }
    )


def check_connection() -> None:
    """1. Whether the Qdrant connection is available."""
    settings = get_settings()
    try:
        client = create_client()
        client.close()
        _record(
            "Qdrant connection",
            True,
            f"Connected to {settings.qdrant_endpoint}",
        )
    except Exception as error:
        _record(
            "Qdrant connection",
            False,
            f"Cannot connect to {settings.qdrant_endpoint}",
            str(error),
        )


def check_collection_exists() -> None:
    """2. Whether the expected collection exists."""
    settings = get_settings()
    client = create_client()
    try:
        exists = client.collection_exists(settings.QDRANT_COLLECTION)
        _record(
            "Collection exists",
            exists,
            f"Collection '{settings.QDRANT_COLLECTION}'",
            "" if exists else "collection not found",
        )
    except Exception as error:
        _record(
            "Collection exists",
            False,
            f"Collection '{settings.QDRANT_COLLECTION}'",
            str(error),
        )
    finally:
        client.close()


def check_collection_has_points() -> None:
    """3. Whether the collection contains points."""
    settings = get_settings()
    client = create_client()
    try:
        if not client.collection_exists(settings.QDRANT_COLLECTION):
            _record(
                "Collection has points",
                False,
                "Skipped: collection does not exist",
                "collection not found",
            )
            return
        count_result = client.count(settings.QDRANT_COLLECTION, exact=True)
        count = count_result.count
        _record(
            "Collection has points",
            count > 0,
            f"{count} points in '{settings.QDRANT_COLLECTION}'",
            "" if count > 0 else "collection is empty",
        )
    except Exception as error:
        _record(
            "Collection has points",
            False,
            f"Collection '{settings.QDRANT_COLLECTION}'",
            str(error),
        )
    finally:
        client.close()


def check_payload_indexes() -> None:
    """4 & 5. Whether all required payload indexes exist and which are missing."""
    settings = get_settings()
    client = create_client()
    try:
        if not client.collection_exists(settings.QDRANT_COLLECTION):
            _record(
                "Payload indexes",
                False,
                "Skipped: collection does not exist",
                "collection not found",
            )
            return

        collection_info = client.get_collection(settings.QDRANT_COLLECTION)
        existing_indexes = set()
        if hasattr(collection_info, "payload_schema") and collection_info.payload_schema:
            for field_name in collection_info.payload_schema:
                existing_indexes.add(field_name)

        required = set(PAYLOAD_INDEXES.keys())
        missing = required - existing_indexes
        present = required & existing_indexes

        details = []
        for field in sorted(present):
            schema_type = PAYLOAD_INDEXES[field]
            details.append(f"  [OK] {field} ({schema_type})")
        for field in sorted(missing):
            schema_type = PAYLOAD_INDEXES[field]
            details.append(f"  [MISSING] {field} ({schema_type})")

        detail_str = "\n".join(details) if details else "No indexes defined"

        if missing:
            _record(
                "Payload indexes",
                False,
                f"{len(present)}/{len(required)} indexes present; {len(missing)} missing\n{detail_str}",
                f"Missing indexes: {', '.join(sorted(missing))}",
            )
        else:
            _record(
                "Payload indexes",
                True,
                f"All {len(required)} indexes present\n{detail_str}",
            )
    except Exception as error:
        _record(
            "Payload indexes",
            False,
            f"Collection '{settings.QDRANT_COLLECTION}'",
            str(error),
        )
    finally:
        client.close()


def check_collection_configuration() -> None:
    """6. Whether the collection configuration is compatible with retrieval."""
    settings = get_settings()
    expected_dim = embedding_dimension()
    client = create_client()
    try:
        if not client.collection_exists(settings.QDRANT_COLLECTION):
            _record(
                "Collection configuration",
                False,
                "Skipped: collection does not exist",
                "collection not found",
            )
            return

        info = client.get_collection(settings.QDRANT_COLLECTION)
        params = info.config.params

        vectors = params.vectors
        if isinstance(vectors, dict):
            vectors = next(iter(vectors.values()), None)

        actual_dim = getattr(vectors, "size", None) if vectors else None
        dim_ok = actual_dim == expected_dim if actual_dim is not None else False

        details = [
            f"  Expected dimension: {expected_dim}",
            f"  Actual dimension:   {actual_dim}",
        ]

        if not dim_ok:
            _record(
                "Collection configuration",
                False,
                "Vector dimension mismatch\n" + "\n".join(details),
                f"Expected {expected_dim}-dimensional vectors, got {actual_dim}",
            )
        else:
            _record(
                "Collection configuration",
                True,
                "Compatible with retrieval implementation\n" + "\n".join(details),
            )
    except Exception as error:
        _record(
            "Collection configuration",
            False,
            f"Collection '{settings.QDRANT_COLLECTION}'",
            str(error),
        )
    finally:
        client.close()


def _print_report() -> None:
    print("\n" + "=" * 60)
    print("QDRANT HEALTH CHECK REPORT")
    print("=" * 60)

    all_pass = True
    for check in _checks:
        status = "PASS" if check["ok"] else "FAIL"
        if not check["ok"]:
            all_pass = False
        print(f"  [{status}] {check['name']}")
        if check["detail"]:
            print(f"         {check['detail']}")
        if check["error"]:
            print(f"         ERROR: {check['error']}")

    print("=" * 60)
    print(f"Overall: {'ALL CHECKS PASSED' if all_pass else 'SOME CHECKS FAILED'}")
    print("=" * 60 + "\n")

    sys.exit(0 if all_pass else 1)


def main() -> None:
    settings = get_settings()
    print(f"Environment: {settings.APP_ENV}")
    print(f"Qdrant endpoint: {settings.qdrant_endpoint}")
    print(f"Collection: {settings.QDRANT_COLLECTION}")
    print(f"Expected vector dimension: {embedding_dimension()}")
    print(f"Required payload indexes: {len(PAYLOAD_INDEXES)} fields")
    print()

    check_connection()
    check_collection_exists()
    check_collection_has_points()
    check_payload_indexes()
    check_collection_configuration()

    _print_report()


if __name__ == "__main__":
    main()