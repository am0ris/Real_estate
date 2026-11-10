"""Shared ingestion, indexing, retrieval, reranking, and generation workflow."""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Iterable

from app.database.vector_db import (
    DEFAULT_COLLECTION,
    create_client,
    ensure_collection,
    rebuild_collection,
    upsert_chunks,
)
from app.helpers.config import get_settings
from app.observability.timing import stage
from app.rag.embeddings import EmbeddingModel
from app.rag.generation import generate_response
from app.rag.retrieval.hybrid_search import SearchResult, hybrid_search
from app.rag.retrieval.metadata_filter import build_metadata_filter
from app.rag.retrieval.reranker import rerank


def _collection_name(collection_name: str | None = None) -> str:
    return collection_name or get_settings().QDRANT_COLLECTION or DEFAULT_COLLECTION


def chunks_from_ingestion(result: dict[str, Any]) -> list[dict[str, Any]]:
    if "results" in result:
        return [
            chunk
            for file_result in result["results"]
            if file_result.get("status") == "success"
            for chunk in file_result.get("chunks", [])
        ]
    return list(result.get("chunks", []))


def index_chunks(
    chunks: Iterable[dict[str, Any]],
    *,
    collection_name: str | None = None,
    rebuild: bool = False,
) -> int:
    records = [
        chunk for chunk in chunks
        if isinstance(chunk.get("text"), str) and chunk["text"].strip()
    ]
    if not records:
        return 0

    vectors = EmbeddingModel().embed_texts([chunk["text"] for chunk in records])
    if len(vectors) != len(records):
        raise RuntimeError("Embedding output count does not match chunk count")

    name = _collection_name(collection_name)
    client = create_client()
    try:
        if rebuild:
            rebuild_collection(client, name)
        else:
            ensure_collection(client, name)
        return upsert_chunks(client, name, records, vectors)
    finally:
        client.close()


def public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Listing metadata for visitors: without the ingesting machine's file paths."""
    return {key: value for key, value in (metadata or {}).items() if key != "source_path"}


def public_source(source_file: str | None) -> str | None:
    """Just the file name of the source (never a directory on the ingesting machine)."""
    return PureWindowsPath(source_file).name if source_file else None


def _candidate_dict(candidate: SearchResult) -> dict[str, Any]:
    return {
        "property_id": candidate.property_id,
        "chunk_id": candidate.chunk_id,
        "text": candidate.text,
        "metadata": public_metadata(candidate.metadata),
        "source_file": public_source(candidate.source_file),
        "score": candidate.combined_score,
    }


# Properties the public catalogue pages through (the website filters client-side).
# Qdrant scrolls in pages; the cap keeps the response small enough for a free instance.
CATALOGUE_PAGE_SIZE = 100
CATALOGUE_MAX_PROPERTIES = 500

# Ingestion bookkeeping that must not reach visitors (it contains local file paths).
_INTERNAL_FIELDS = ("source_path", "file_name", "file_extension", "source_type")


def is_demo_listing(*sources: Any) -> bool:
    """True when a listing came from a test/demo file (see Settings.DEMO_SOURCE_MARKERS).

    ``sources`` are file names or paths (Windows or POSIX); only the file name is matched.
    """
    markers = get_settings().demo_markers
    for source in sources:
        if not source:
            continue
        name = PureWindowsPath(str(source)).name.lower()  # handles "a\b.csv" and "a/b.csv"
        if any(marker in name for marker in markers):
            return True
    return False


def _candidate_is_demo(candidate: SearchResult) -> bool:
    metadata = candidate.metadata or {}
    return is_demo_listing(candidate.source_file, metadata.get("file_name"), metadata.get("source_path"))


def search_index(
    query: str,
    requirements: dict[str, Any] | None = None,
    *,
    top_k: int = 5,
    retrieval_top_k: int = 20,
    collection_name: str | None = None,
) -> list[SearchResult]:
    query = query.strip()
    if not query:
        raise ValueError("query cannot be blank")
    filt = build_metadata_filter(requirements or {})
    with stage("qdrant_connect"):
        client = create_client()
    try:
        candidates = hybrid_search(
            query=query,
            requirements_filter=filt,
            collection_name=_collection_name(collection_name),
            qdrant_client=client,
            top_k=retrieval_top_k,
        )
    finally:
        client.close()
    # Test listings never reach customers unless demo mode is on - and then they are marked.
    if get_settings().SHOW_DEMO_LISTINGS:
        for candidate in candidates:
            if _candidate_is_demo(candidate):
                candidate.metadata = {**candidate.metadata, "is_demo": True}
    else:
        candidates = [candidate for candidate in candidates if not _candidate_is_demo(candidate)]
    with stage("rerank"):
        return rerank(query, candidates, top_k=top_k)


def answer_query(
    query: str,
    requirements: dict[str, Any] | None = None,
    *,
    top_k: int = 5,
    retrieval_top_k: int = 20,
    conversation_history: list[dict[str, str]] | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    candidates = search_index(
        query,
        requirements,
        top_k=top_k,
        retrieval_top_k=retrieval_top_k,
    )
    generated = generate_response(
        customer_query=query,
        candidates=candidates,
        requirements=requirements or {},
        conversation_history=conversation_history,
        channel=channel,
    )
    return {
        "answer": generated.answer_text,
        "cited_property_ids": generated.cited_property_ids,
        "grounding_property_ids": generated.grounding_property_ids,
        "properties": [_candidate_dict(candidate) for candidate in candidates],
    }


def _search_result_from_dict(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        property_id=str(item["property_id"]) if item.get("property_id") is not None else None,
        chunk_id=item.get("chunk_id"),
        text=str(item.get("text") or ""),
        metadata=dict(item.get("metadata") or {}),
        combined_score=float(item.get("score") or 0.0),
        source_file=item.get("source_file"),
    )


def answer_about_properties(
    query: str,
    properties: list[dict[str, Any]],
    requirements: dict[str, Any] | None = None,
    *,
    conversation_history: list[dict[str, str]] | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Answer a follow-up using already-retrieved properties as the only evidence.

    Used when the customer asks about a property they already selected, so the
    answer stays grounded in that listing instead of running a fresh search.
    """
    candidates = [_search_result_from_dict(item) for item in properties]
    generated = generate_response(
        customer_query=query,
        candidates=candidates,
        requirements=requirements or {},
        conversation_history=conversation_history,
        channel=channel,
    )
    return {
        "answer": generated.answer_text,
        "cited_property_ids": generated.cited_property_ids,
        "grounding_property_ids": generated.grounding_property_ids,
        "properties": [_candidate_dict(candidate) for candidate in candidates],
    }


def list_indexed_properties(
    *, collection_name: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    show_demo = get_settings().SHOW_DEMO_LISTINGS
    name = _collection_name(collection_name)
    properties: dict[str, dict[str, Any]] = {}
    client = create_client()
    try:
        offset = None
        while len(properties) < CATALOGUE_MAX_PROPERTIES:
            points, offset = client.scroll(
                collection_name=name,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = getattr(point, "payload", None) or {}
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                merged = {**metadata, **{key: value for key, value in payload.items() if key != "metadata"}}
                demo = is_demo_listing(merged.get("file_name"), merged.get("source_path"))
                if demo and not show_demo:
                    continue
                property_id = str(merged.get("property_id") or merged.get("id") or point.id)
                record = {key: value for key, value in merged.items() if key not in _INTERNAL_FIELDS}
                record["property_id"] = property_id
                if demo:
                    record["is_demo"] = True
                properties.setdefault(property_id, record)
            if offset is None:
                break
    finally:
        client.close()
    return list(properties.values())[:CATALOGUE_MAX_PROPERTIES]
