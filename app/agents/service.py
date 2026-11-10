"""Single entry point that runs one customer turn through the agent graph.

Website text, website voice (via the Gemini Live tool bridge) and WhatsApp
all call ``conversation_service.run_turn`` with a per-customer ``thread_id``,
so every channel shares one LangGraph workflow and one checkpointed state.

Storage: with PostgreSQL configured (always, in production) the checkpointer is
``PostgresSaver``, so conversation memory survives restarts and is shared by all
API workers; a per-conversation advisory lock keeps two workers from running
turns of the same conversation at once. Without a database (local development
and tests only) it falls back to LangGraph's in-process ``MemorySaver``.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.checkpointer import build_checkpointer, prune_thread, thread_lock
from app.agents.graph import build_graph
from app.agents.state import describe_requirements, property_label
from app.agents.tools.crm import crm_service
from app.database.session import is_postgres
from app.helpers.config import get_settings
from app.observability.timing import format_timings, stage, start_turn


logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2_000


@dataclass
class TurnResult:
    thread_id: str
    intent: str
    agent: str
    response: str
    language: str
    properties: list[dict[str, Any]] = field(default_factory=list)
    cited_property_ids: list[str] = field(default_factory=list)
    grounding_property_ids: list[str] = field(default_factory=list)
    requirements: dict[str, Any] = field(default_factory=dict)
    qualification: dict[str, Any] = field(default_factory=dict)
    selected_property_id: str | None = None
    appointment: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    scheduling_stage: str | None = None
    error: str | None = None
    # Seconds spent per stage of this turn (embed, qdrant, rerank, generate, lock_wait, ...).
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data.pop("timings", None)  # internal: not part of what is sent to clients
        return data


class ConversationService:

    def __init__(self, checkpointer: Any = None) -> None:
        # An explicit checkpointer (tests) keeps the service self-contained;
        # otherwise use the shared durable one.
        self._durable = checkpointer is None and is_postgres()
        self._graph = build_graph(checkpointer if checkpointer is not None else build_checkpointer())
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

    def _lock_for(self, thread_id: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(thread_id, Lock())

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def _exclusive(self, thread_id: str) -> ExitStack:
        """One turn at a time per conversation: in this process and across workers."""
        stack = ExitStack()
        stack.enter_context(self._lock_for(thread_id))
        # The database lock holds one pooled connection for the whole turn, model calls included,
        # so it caps how many conversations can run at once. It is only needed with several workers.
        if self._durable and get_settings().TURN_LOCK_BACKEND == "postgres":
            stack.enter_context(thread_lock(thread_id))
        return stack

    def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        channel: str,
        customer_id: str,
        modality: str = "text",
        language: str | None = None,
        clear_requirements: list[str] | None = None,
    ) -> TurnResult:
        message = (text or "").strip()
        if not message:
            raise ValueError("message cannot be blank")
        message = message[:MAX_MESSAGE_CHARS]

        turn_input: dict[str, Any] = {
            "messages": [HumanMessage(content=message)],
            "query": message,
            "channel": channel,
            "modality": modality,
            "customer_id": customer_id,
            "clear_requirements": list(clear_requirements or []),
            # Per-turn outputs start empty so stale results are never re-sent.
            "response": "",
            "turn_properties": [],
            "cited_property_ids": [],
            "grounding_property_ids": [],
            "error": None,
        }
        if language in {"ar", "en"}:
            turn_input["language"] = language

        timings = start_turn()
        started = time.perf_counter()
        # A voice tool call and a typed message for the same customer must not
        # interleave state updates (in this process, and across workers when TURN_LOCK_BACKEND=postgres).
        lock_requested = time.perf_counter()
        with self._exclusive(thread_id):
            timings["lock_wait"] = time.perf_counter() - lock_requested
            with stage("graph"):
                state = self._graph.invoke(turn_input, self._config(thread_id))
            if self._durable:
                with stage("prune"):
                    prune_thread(thread_id)

        result = self._result(thread_id, state)
        # The conversation is already saved; CRM bookkeeping must never fail a turn.
        try:
            with stage("crm_sync"):
                crm_service.sync_conversation(customer_id, state)
        except Exception:
            logger.exception("Failed to sync CRM | thread=%s", thread_id)
        try:
            with stage("interaction"):
                crm_service.record_interaction(
                    customer_id=customer_id,
                    channel=f"{channel}:{modality}",
                    content=message,
                    response=result.response,
                )
        except Exception:
            logger.exception("Failed to record interaction | thread=%s", thread_id)

        # "graph" contains the model/search stages; what is left of it is the conversation
        # state read/write in the database and the agents' own work.
        inner = ("embed", "qdrant_connect", "qdrant", "rerank", "generate", "chat_llm")
        timings["graph_other"] = max(0.0, timings.pop("graph", 0.0) - sum(timings.get(name, 0.0) for name in inner))
        timings["total"] = time.perf_counter() - started
        result.timings = {name: round(seconds, 3) for name, seconds in timings.items()}
        logger.info("Turn timings | channel=%s intent=%s %s", channel, result.intent, format_timings(timings))
        return result

    @staticmethod
    def _result(thread_id: str, state: dict[str, Any]) -> TurnResult:
        selected = state.get("selected_property") or {}
        return TurnResult(
            thread_id=thread_id,
            intent=state.get("intent", "conversational"),
            agent=state.get("active_agent", "conversational"),
            response=state.get("response", ""),
            language=state.get("language", "en"),
            properties=list(state.get("turn_properties") or []),
            cited_property_ids=list(state.get("cited_property_ids") or []),
            grounding_property_ids=list(state.get("grounding_property_ids") or []),
            requirements=dict(state.get("requirements") or {}),
            qualification=dict(state.get("qualification") or {}),
            selected_property_id=str(selected["property_id"]) if selected.get("property_id") is not None else None,
            appointment=state.get("appointment"),
            handoff=state.get("handoff"),
            scheduling_stage=(state.get("scheduling") or {}).get("stage"),
            error=state.get("error"),
        )

    def get_state(self, thread_id: str) -> dict[str, Any]:
        snapshot = self._graph.get_state(self._config(thread_id))
        return dict(snapshot.values or {})

    def reset_search(self, thread_id: str) -> None:
        """Start a new search but keep the customer's profile and contact."""
        if not self.get_state(thread_id):
            return
        with self._exclusive(thread_id):
            self._graph.update_state(
                self._config(thread_id),
                {"requirements": {}, "candidates": [], "selected_property": None, "scheduling": {}},
                as_node="record_reply",
            )
            if self._durable:
                prune_thread(thread_id)

    def context_summary(self, thread_id: str, *, max_messages: int = 6) -> str:
        """Plain-text recap of a conversation, used to brief the voice model."""
        state = self.get_state(thread_id)
        if not state:
            return ""
        lines: list[str] = []
        known = describe_requirements(state.get("requirements") or {}, state.get("qualification") or {}, "en")
        if known:
            lines.append(f"Known customer requirements: {known}.")
        if state.get("selected_property"):
            lines.append(f"Property currently discussed: {property_label(state['selected_property'])}.")
        if state.get("candidates"):
            listed = "; ".join(f"{index}. {property_label(item)}" for index, item in enumerate(state["candidates"][:5], start=1))
            lines.append(f"Last properties shown: {listed}.")
        recent = []
        for message in (state.get("messages") or [])[-max_messages:]:
            role = {"human": "Customer", "ai": "Assistant"}.get(getattr(message, "type", ""), "")
            if role:
                recent.append(f"{role}: {str(message.content)[:300]}")
        if recent:
            lines.append("Recent conversation:\n" + "\n".join(recent))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lazy process-wide instance
# ---------------------------------------------------------------------------
#
# Building the service opens the database connection pool, so it must not happen
# at import time (tests, tooling and `alembic` import this package).

_instance: ConversationService | None = None
_instance_guard = Lock()


def get_conversation_service() -> ConversationService:
    global _instance
    if _instance is None:
        with _instance_guard:
            if _instance is None:
                _instance = ConversationService()
    return _instance


def reset_conversation_service() -> None:
    global _instance
    with _instance_guard:
        _instance = None


class _LazyConversationService:
    """Forwards attribute access to the real service, creating it on first use."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_conversation_service(), name)


conversation_service = _LazyConversationService()
