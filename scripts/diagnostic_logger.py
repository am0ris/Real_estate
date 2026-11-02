"""Temporary diagnostic logging for WhatsApp vs Website RAG comparison.

Logs structured key-value pairs at each stage of the retrieval pipeline
so the two channels can be compared side by side.

No secrets, phone numbers, or personal data are logged.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger("rag_diagnostic")


def _mask_id(value: str) -> str:
    """Return a short hash-safe identifier for logging."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def log_request_received(channel: str, session_id: str, query: str) -> None:
    """Log the incoming request."""
    logger.info(
        "DIAG request_received channel=%s session=%s query_len=%d",
        channel,
        _mask_id(session_id),
        len(query),
    )


def log_retrieval_stage(
    *,
    channel: str,
    session_id: str,
    query: str,
    requirements: dict[str, Any],
    collection_name: str,
    property_ids: list[str],
    document_ids: list[str],
    scores: list[float],
    filter_description: str,
    rerank_applied: bool,
    rerank_count: int,
    context_length: int,
    llm_model: str,
    history_length: int,
    status: str,
    error: str | None = None,
) -> None:
    """Log the complete retrieval pipeline result for one turn."""
    logger.info(
        "DIAG retrieval channel=%s session=%s collection=%s filters=%s "
        "doc_count=%d prop_ids=%s doc_ids=%s scores=%s rerank=%s rerank_n=%d "
        "context_len=%d llm=%s history_len=%d status=%s error=%s",
        channel,
        _mask_id(session_id),
        collection_name,
        filter_description,
        len(property_ids),
        property_ids,
        document_ids,
        scores,
        rerank_applied,
        rerank_count,
        context_length,
        llm_model,
        history_length,
        status,
        error or "",
    )