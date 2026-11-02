"""Qdrant client and collection operations for RAG indexing."""

from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from app.helpers.config import get_settings
from app.rag.embeddings import embedding_dimension


logger = logging.getLogger(__name__)

# Same default as Settings.QDRANT_COLLECTION, so code and .env agree.
DEFAULT_COLLECTION = "properties"

# Seconds before a Qdrant request is abandoned (cold free-tier clusters are slow).
QDRANT_TIMEOUT_SECONDS = 20


class VectorDimensionMismatch(RuntimeError):
    """The collection was built with a different embedding size than configured."""

_POINT_ID_NAMESPACE = NAMESPACE_URL

# Keep requests comfortably below Qdrant Cloud's 32 MB request limit.
UPSERT_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Payload indexes
# ---------------------------------------------------------------------------

# Fields used by metadata filtering.
#
# Qdrant requires a payload index for filtered search. Keep the schema
# explicit so the collection has predictable filter behavior.
PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "location": models.PayloadSchemaType.KEYWORD,
    "city": models.PayloadSchemaType.KEYWORD,
    "district": models.PayloadSchemaType.KEYWORD,
    "neighborhood": models.PayloadSchemaType.KEYWORD,
    "compound": models.PayloadSchemaType.KEYWORD,
    "property_type": models.PayloadSchemaType.KEYWORD,
    "transaction_type": models.PayloadSchemaType.KEYWORD,
    "developer": models.PayloadSchemaType.KEYWORD,
    "finishing": models.PayloadSchemaType.KEYWORD,
    "delivery": models.PayloadSchemaType.KEYWORD,
    "availability": models.PayloadSchemaType.KEYWORD,
    "currency": models.PayloadSchemaType.KEYWORD,
    "payment_plan": models.PayloadSchemaType.KEYWORD,
    "bedrooms": models.PayloadSchemaType.INTEGER,
    "bathrooms": models.PayloadSchemaType.INTEGER,
    "price": models.PayloadSchemaType.FLOAT,
    "area": models.PayloadSchemaType.FLOAT,
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def create_client(
    url: str | None = None,
    api_key: str | None = None,
) -> QdrantClient:
    """Create a Qdrant client from the application settings (``.env`` included)."""
    settings = get_settings()
    return QdrantClient(
        url=url or settings.qdrant_endpoint,
        api_key=api_key if api_key is not None else settings.QDRANT_API_KEY,
        timeout=QDRANT_TIMEOUT_SECONDS,
        # The client warns when its version differs from the cluster's; the
        # pinned range is verified against Qdrant Cloud.
        check_compatibility=False,
    )


def collection_vector_size(client: QdrantClient, collection_name: str) -> int | None:
    """Vector size of an existing collection, or None if it does not exist."""
    if not client.collection_exists(collection_name):
        return None
    vectors = client.get_collection(collection_name).config.params.vectors
    if isinstance(vectors, dict):  # named vectors: use the only/first one
        vectors = next(iter(vectors.values()), None)
    return getattr(vectors, "size", None)


def check_collection_dimension(client: QdrantClient, collection_name: str) -> None:
    """Fail loudly if the collection's vector size is not the configured one.

    Mixing embedding models silently ruins search quality (or Qdrant rejects the
    query with an opaque error), so this is checked before indexing and at
    readiness time.
    """
    size = collection_vector_size(client, collection_name)
    expected = embedding_dimension()
    if size is not None and size != expected:
        raise VectorDimensionMismatch(
            f"Collection '{collection_name}' has {size}-dimensional vectors but "
            f"EMBEDDING_DIMENSION={expected}. Re-index into a new collection "
            f"(scripts/ingest.py --rebuild) or set QDRANT_COLLECTION to a fresh name."
        )


def describe_collection() -> dict[str, Any]:
    """Readiness probe for /health/ready; never raises, never leaks credentials."""
    settings = get_settings()
    report: dict[str, Any] = {"ok": False, "collection": settings.QDRANT_COLLECTION, "expected_dimension": embedding_dimension()}
    client = None
    try:
        client = create_client()
        size = collection_vector_size(client, settings.QDRANT_COLLECTION)
        if size is None:
            report["error"] = "collection does not exist: run scripts/ingest.py"
        elif size != embedding_dimension():
            report.update(actual_dimension=size, error="vector size mismatch: re-index the collection")
        else:
            report.update(ok=True, actual_dimension=size, points=client.count(settings.QDRANT_COLLECTION, exact=False).count)
    except Exception as error:
        report["error"] = f"qdrant unreachable ({type(error).__name__})"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return report


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def ensure_payload_indexes(
    client: QdrantClient,
    collection_name: str,
) -> dict[str, str]:
    """Ensure all metadata-filter fields have Qdrant payload indexes.

    Idempotent: indexes that already exist are skipped.
    Returns a dict mapping field name to one of:
        "created", "already_exists", "failed".
    """

    results: dict[str, str] = {}
    for field_name, schema_type in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema_type,
                wait=True,
            )
            results[field_name] = "created"
            logger.info("Payload index created | collection=%s field=%s", collection_name, field_name)
        except Exception as error:
            message = str(error)
            # Qdrant returns a 409 Conflict / "already exists" when the index
            # is already present; treat that as success.
            if "already exist" in message.lower() or "already exists" in message.lower():
                results[field_name] = "already_exists"
                logger.debug(
                    "Payload index already exists | collection=%s field=%s",
                    collection_name,
                    field_name,
                )
            else:
                results[field_name] = "failed"
                logger.exception(
                    "Payload index creation failed | collection=%s field=%s error=%s",
                    collection_name,
                    field_name,
                    error,
                )
    return results


def ensure_collection(
    client: QdrantClient,
    collection_name: str = DEFAULT_COLLECTION,
) -> None:
    """Ensure the collection and its metadata indexes exist."""

    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=embedding_dimension(),
                distance=models.Distance.COSINE,
            ),
        )
    else:
        check_collection_dimension(client, collection_name)

    index_results = ensure_payload_indexes(client, collection_name)
    created = [f for f, r in index_results.items() if r == "created"]
    skipped = [f for f, r in index_results.items() if r == "already_exists"]
    failed = [f for f, r in index_results.items() if r == "failed"]
    if created:
        logger.info(
            "Payload indexes created | collection=%s indexes=%s",
            collection_name,
            ", ".join(created),
        )
    if skipped:
        logger.debug(
            "Payload indexes already exist | collection=%s indexes=%s",
            collection_name,
            ", ".join(skipped),
        )
    if failed:
        logger.warning(
            "Payload indexes failed | collection=%s indexes=%s",
            collection_name,
            ", ".join(failed),
        )


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    chunks: Iterable[dict[str, Any]],
    vectors: Iterable[list[float]],
) -> int:
    """Upsert embedded chunks in batches with searchable metadata payloads."""

    chunk_list = list(chunks)
    vector_list = list(vectors)

    if len(chunk_list) != len(vector_list):
        raise ValueError(
            f"Chunk/vector count mismatch: {len(chunk_list)} chunks, "
            f"{len(vector_list)} vectors"
        )

    points: list[models.PointStruct] = []

    for chunk, vector in zip(chunk_list, vector_list):
        chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")

        if not chunk_id:
            raise ValueError("Each chunk must include a deterministic id")

        metadata = chunk.get("metadata") or {}

        if not isinstance(metadata, dict):
            raise TypeError("Chunk metadata must be a dictionary")

        if len(vector) != embedding_dimension():
            raise ValueError(
                f"Expected {embedding_dimension()}-dimensional vector, "
                f"got {len(vector)}"
            )

        payload = {
            **metadata,
            "text": chunk.get("text", ""),
            "id": chunk_id,
            "chunk_id": chunk_id,
            "metadata": metadata,
        }

        # Qdrant accepts unsigned integers or UUIDs as point IDs.
        # Derive a stable UUID from the original chunk ID so arbitrary
        # deterministic IDs remain repeatable across indexing runs.
        point_id = str(uuid5(_POINT_ID_NAMESPACE, chunk_id))

        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload,
            )
        )

    if not points:
        return 0

    total = 0

    for start in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[start : start + UPSERT_BATCH_SIZE]

        client.upsert(
            collection_name=collection_name,
            points=batch,
            wait=True,
        )

        total += len(batch)

    return total


# ---------------------------------------------------------------------------
# Collection lifecycle
# ---------------------------------------------------------------------------


def delete_collection(
    client: QdrantClient,
    collection_name: str,
) -> None:
    """Delete a collection if it exists."""

    if client.collection_exists(collection_name):
        client.delete_collection(collection_name)


def rebuild_collection(
    client: QdrantClient,
    collection_name: str = DEFAULT_COLLECTION,
) -> None:
    """Drop and recreate a collection with the configured schema."""

    delete_collection(client, collection_name)
    ensure_collection(client, collection_name)