"""Hybrid retrieval for the real-estate RAG system.

This module combines:
- Metadata filtering
- Dense vector search via Qdrant
- Optional BM25 lexical retrieval
- Score normalization
- Weighted hybrid ranking
- Query embeddings via shared EmbeddingModel

Design principles:
- Query embeddings use the same EmbeddingModel used during indexing.
- Metadata filters are applied to both semantic and lexical retrieval.
- BM25 is optional and only runs when an explicit in-memory corpus is provided.
- When no corpus is provided, retrieval degrades gracefully to semantic-only search.
- The module does not automatically load the entire Qdrant collection into memory.
"""

from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi

from app.observability.timing import stage

from ..embeddings import EmbeddingModel
from .metadata_filter import MetadataFilter, matches


# ---------------------------------------------------------------------------
# Shared embedding model
# ---------------------------------------------------------------------------

_embedding_model: EmbeddingModel | None = None


def _get_embedding_model() -> EmbeddingModel:
    """Return the shared embedding model instance."""

    global _embedding_model

    if _embedding_model is None:
        _embedding_model = EmbeddingModel()

    return _embedding_model


def embed_query(query: str) -> list[float]:
    """Embed a user query using the shared embedding model."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")

    return _get_embedding_model().embed_query(query.strip())


# ---------------------------------------------------------------------------
# Search result
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """Normalized retrieval result."""

    property_id: str | None
    chunk_id: str | None
    text: str
    metadata: dict[str, Any]
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    combined_score: float = 0.0
    source_file: str | None = None


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _flatten_metadata(
    payload_or_chunk: dict[str, Any],
) -> dict[str, Any]:
    """Flatten nested metadata while preserving top-level payload fields."""

    nested_metadata = payload_or_chunk.get("metadata")

    if isinstance(nested_metadata, dict):
        flattened = dict(nested_metadata)
    else:
        flattened = {}

    for key, value in payload_or_chunk.items():
        if key != "metadata":
            flattened[key] = value

    return flattened


def _get_source_file(
    payload_or_chunk: dict[str, Any],
    metadata: dict[str, Any],
) -> str | None:
    """Extract the best available source-file identifier.

    Preference order:
    1. source_file
    2. file_name
    3. source_path

    ``source_file`` is the canonical SearchResult field.
    ``file_name`` and ``source_path`` are common fields produced by
    the ingestion layer and stored in Qdrant.
    """

    source_file = payload_or_chunk.get("source_file")

    if source_file is None:
        source_file = metadata.get("source_file")

    if source_file is None:
        source_file = payload_or_chunk.get("file_name")

    if source_file is None:
        source_file = metadata.get("file_name")

    if source_file is None:
        source_file = payload_or_chunk.get("source_path")

    if source_file is None:
        source_file = metadata.get("source_path")

    if source_file is None:
        return None

    return str(source_file)


def _result_key(result: SearchResult) -> str:
    """Return a stable key for merging semantic and lexical results."""

    if result.chunk_id:
        return str(result.chunk_id)

    if result.property_id:
        return str(result.property_id)

    return f"__synthetic__{id(result)}"


# ---------------------------------------------------------------------------
# Qdrant filtering
# ---------------------------------------------------------------------------


def _to_qdrant_filter(
    requirements_filter: MetadataFilter | None,
) -> dict[str, Any] | None:
    """Convert MetadataFilter constraints to Qdrant filter syntax."""

    if requirements_filter is None:
        return None

    conditions: list[dict[str, Any]] = []

    # MetadataFilter uses ``equality`` as a list of EqualityConstraint
    # objects, not an ``equalities`` dictionary.
    for constraint in requirements_filter.equality:
        conditions.append(
            {
                "key": constraint.field,
                "match": {
                    "value": constraint.value,
                },
            }
        )

    for constraint in requirements_filter.ranges:
        range_condition: dict[str, Any] = {}

        if constraint.gte is not None:
            range_condition["gte"] = constraint.gte

        if constraint.lte is not None:
            range_condition["lte"] = constraint.lte

        if range_condition:
            conditions.append(
                {
                    "key": constraint.field,
                    "range": range_condition,
                }
            )

    if not conditions:
        return None

    return {"must": conditions}


# ---------------------------------------------------------------------------
# Qdrant semantic search
# ---------------------------------------------------------------------------


def _qdrant_search(
    query: str,
    requirements_filter: MetadataFilter | None,
    collection_name: str,
    qdrant_client: Any,
    top_k: int = 10,
) -> list[SearchResult]:
    """Run dense semantic search against Qdrant."""

    if top_k <= 0:
        return []

    if qdrant_client is None:
        raise ValueError("qdrant_client is required")

    qdrant_filter = _to_qdrant_filter(requirements_filter)

    with stage("embed"):
        query_vector = embed_query(query)

    search_kwargs: dict[str, Any] = {
        "collection_name": collection_name,
        "limit": top_k,
        "query_vector": query_vector,
    }

    if qdrant_filter is not None:
        search_kwargs["query_filter"] = qdrant_filter

    with stage("qdrant"):
        points = qdrant_client.search(**search_kwargs)

    results: list[SearchResult] = []

    for point in points:
        payload = getattr(point, "payload", None) or {}

        if not isinstance(payload, dict):
            payload = {}

        score = float(getattr(point, "score", 0.0) or 0.0)

        flattened_metadata = _flatten_metadata(payload)

        text = payload.get("text", "")
        if text is None:
            text = ""

        property_id = payload.get(
            "property_id",
            flattened_metadata.get("property_id"),
        )

        chunk_id = payload.get(
            "chunk_id",
            flattened_metadata.get("chunk_id"),
        )

        source_file = _get_source_file(
            payload,
            flattened_metadata,
        )

        results.append(
            SearchResult(
                property_id=(
                    str(property_id)
                    if property_id is not None
                    else None
                ),
                chunk_id=(
                    str(chunk_id)
                    if chunk_id is not None
                    else None
                ),
                text=str(text),
                metadata=flattened_metadata,
                semantic_score=score,
                combined_score=score,
                source_file=source_file,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Requirement matching
# ---------------------------------------------------------------------------


def _matches_requirements(
    chunk: dict[str, Any],
    requirements_filter: MetadataFilter | None,
) -> bool:
    """Check whether a corpus chunk satisfies metadata requirements."""

    if requirements_filter is None:
        return True

    flattened_metadata = _flatten_metadata(chunk)

    return matches(
        flattened_metadata,
        requirements_filter,
    )


# ---------------------------------------------------------------------------
# BM25 lexical search
# ---------------------------------------------------------------------------


def _bm25_search(
    query: str,
    corpus_chunks: list[dict[str, Any]],
    requirements_filter: MetadataFilter | None = None,
    top_k: int = 10,
) -> list[SearchResult]:
    """Run BM25 lexical search over an explicit in-memory corpus."""

    if top_k <= 0:
        return []

    if not corpus_chunks:
        return []

    filtered_chunks = [
        chunk
        for chunk in corpus_chunks
        if isinstance(chunk, dict)
        and _matches_requirements(
            chunk,
            requirements_filter,
        )
    ]

    if not filtered_chunks:
        return []

    documents: list[str] = []

    for chunk in filtered_chunks:
        text = chunk.get("text", "")

        if text is None:
            text = ""

        documents.append(str(text))

    tokenized_documents = [
        document.lower().split()
        for document in documents
    ]

    tokenized_query = query.lower().split()

    if not tokenized_query:
        return []

    bm25 = BM25Okapi(tokenized_documents)
    scores = bm25.get_scores(tokenized_query)

    ranked_indices = sorted(
        range(len(scores)),
        key=lambda index: scores[index],
        reverse=True,
    )

    results: list[SearchResult] = []

    for index in ranked_indices:
        score = float(scores[index])

        if score <= 0:
            continue

        chunk = filtered_chunks[index]

        flattened_metadata = _flatten_metadata(chunk)

        property_id = chunk.get(
            "property_id",
            flattened_metadata.get("property_id"),
        )

        chunk_id = chunk.get(
            "chunk_id",
            flattened_metadata.get("chunk_id"),
        )

        text = chunk.get("text", "")
        if text is None:
            text = ""

        source_file = _get_source_file(
            chunk,
            flattened_metadata,
        )

        results.append(
            SearchResult(
                property_id=(
                    str(property_id)
                    if property_id is not None
                    else None
                ),
                chunk_id=(
                    str(chunk_id)
                    if chunk_id is not None
                    else None
                ),
                text=str(text),
                metadata=flattened_metadata,
                lexical_score=score,
                combined_score=score,
                source_file=source_file,
            )
        )

        if len(results) >= top_k:
            break

    return results


# ---------------------------------------------------------------------------
# Score normalization
# ---------------------------------------------------------------------------


def _min_max_normalize(scores: list[float]) -> list[float]:
    """Normalize scores to the [0, 1] range."""

    if not scores:
        return []

    minimum = min(scores)
    maximum = max(scores)

    if minimum == maximum:
        return [1.0 for _ in scores]

    return [
        (score - minimum) / (maximum - minimum)
        for score in scores
    ]


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


def hybrid_search(
    query: str,
    requirements_filter: MetadataFilter | None,
    collection_name: str,
    qdrant_client: Any = None,
    top_k: int = 10,
    semantic_weight: float = 0.7,
    lexical_weight: float = 0.3,
    corpus_chunks: list[dict[str, Any]] | None = None,
    *,
    client: Any = None,
) -> list[SearchResult]:
    """Run hybrid semantic + lexical retrieval.

    When ``corpus_chunks`` is None, retrieval gracefully falls back to
    semantic-only Qdrant search.

    ``client`` is accepted as a backwards-compatible alias for
    ``qdrant_client``.
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")

    if top_k <= 0:
        return []

    if qdrant_client is None:
        qdrant_client = client

    if qdrant_client is None:
        raise ValueError("qdrant_client is required")

    if semantic_weight < 0 or lexical_weight < 0:
        raise ValueError("search weights must be non-negative")

    total_weight = semantic_weight + lexical_weight

    if total_weight <= 0:
        raise ValueError(
            "at least one search weight must be greater than zero"
        )

    semantic_weight = semantic_weight / total_weight
    lexical_weight = lexical_weight / total_weight

    # Keep this positional call compatible with lightweight test/service
    # adapters that still use the older ``filt`` parameter name.
    semantic_results = _qdrant_search(
        query,
        requirements_filter,
        collection_name,
        qdrant_client,
        top_k,
    )

    # -----------------------------------------------------------------------
    # Semantic-only fallback
    # -----------------------------------------------------------------------

    if corpus_chunks is None:
        semantic_scores = [
            result.semantic_score
            for result in semantic_results
        ]

        normalized_scores = _min_max_normalize(
            semantic_scores
        )

        for result, normalized_score in zip(
            semantic_results,
            normalized_scores,
        ):
            result.combined_score = normalized_score

        semantic_results.sort(
            key=lambda result: result.combined_score,
            reverse=True,
        )

        return semantic_results[:top_k]

    # -----------------------------------------------------------------------
    # Lexical retrieval
    # -----------------------------------------------------------------------

    lexical_results = _bm25_search(
        query=query,
        corpus_chunks=corpus_chunks,
        requirements_filter=requirements_filter,
        top_k=top_k,
    )

    semantic_scores = [
        result.semantic_score
        for result in semantic_results
    ]

    lexical_scores = [
        result.lexical_score
        for result in lexical_results
    ]

    normalized_semantic = _min_max_normalize(
        semantic_scores
    )

    normalized_lexical = _min_max_normalize(
        lexical_scores
    )

    # -----------------------------------------------------------------------
    # Merge semantic + lexical results
    # -----------------------------------------------------------------------

    merged: dict[str, SearchResult] = {}

    for result, normalized_score in zip(
        semantic_results,
        normalized_semantic,
    ):
        key = _result_key(result)

        result.combined_score = (
            semantic_weight * normalized_score
        )

        merged[key] = result

    for result, normalized_score in zip(
        lexical_results,
        normalized_lexical,
    ):
        key = _result_key(result)

        if key in merged:
            merged[key].lexical_score = result.lexical_score

            merged[key].combined_score += (
                lexical_weight * normalized_score
            )

            if merged[key].source_file is None:
                merged[key].source_file = result.source_file

            if not merged[key].metadata and result.metadata:
                merged[key].metadata = result.metadata

        else:
            result.combined_score = (
                lexical_weight * normalized_score
            )
            merged[key] = result

    final_results = sorted(
        merged.values(),
        key=lambda result: result.combined_score,
        reverse=True,
    )

    return final_results[:top_k]
