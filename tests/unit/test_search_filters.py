"""Hard search filters must be backed by data.

A hard filter on a field that no ingested listing carries returns zero results, and the
assistant then answers "no properties available" however the customer rephrases the search.
That is exactly how ``transaction_type`` (every listing is for sale, but the payload has no such
field) made WhatsApp find nothing. These tests fail if it ever happens again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.state import FILTER_KEYS
from app.agents.subgraphs.property.state import search_filters
from app.database.vector_db import PAYLOAD_INDEXES
from app.rag.retrieval.metadata_filter import REQUIREMENT_TO_METADATA_FIELD

SAMPLE_CSV = Path(__file__).resolve().parents[2] / "data" / "raw" / "properties" / "test_properties_small.csv"


def _payload_field(requirement_key: str) -> str:
    mapping = REQUIREMENT_TO_METADATA_FIELD[requirement_key]
    return mapping[0] if isinstance(mapping, tuple) else mapping


def test_every_filter_key_is_indexed_in_qdrant():
    for key in FILTER_KEYS:
        assert _payload_field(key) in PAYLOAD_INDEXES, f"'{key}' filters on a field with no payload index"


def test_every_filter_key_is_backed_by_ingested_data():
    if not SAMPLE_CSV.exists():
        pytest.skip(f"sample data not found: {SAMPLE_CSV}")
    from app.rag.ingestion.pipeline import ingest_file

    chunks = ingest_file(str(SAMPLE_CSV))["chunks"]
    assert chunks, "the sample file produced no chunks"
    payloads = [chunk["metadata"] for chunk in chunks]
    for key in FILTER_KEYS:
        field = _payload_field(key)
        assert any(payload.get(field) is not None for payload in payloads), (
            f"'{key}' is a hard filter on '{field}', but no ingested listing has that field: "
            "every search that mentions it would return nothing"
        )


def test_buying_or_renting_wording_is_not_a_hard_filter():
    carried = {"district": "New Cairo", "transaction_type": "sale", "max_price": 5_000_000}
    assert search_filters(carried) == {"district": "New Cairo", "max_price": 5_000_000}
