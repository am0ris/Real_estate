"""Where a turn's time goes, the optional database turn lock and the optional reasoning-effort option."""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from app.llm.params import reasoning_params
from app.observability.timing import format_timings, stage, start_turn


# --- stage timer ---------------------------------------------------------------------------------


def test_stage_outside_a_turn_records_nothing_and_does_not_fail():
    with stage("embed"):
        pass  # no turn started in this context: must be a no-op


def test_stage_sums_repeated_steps_of_one_turn():
    timings = start_turn()
    for _ in range(2):
        with stage("embed"):
            time.sleep(0.01)
    assert 0.018 <= timings["embed"] < 0.5


def test_a_stage_running_in_a_worker_thread_is_counted():
    timings = start_turn()

    def work():
        with stage("rerank"):
            time.sleep(0.01)

    context = contextvars.copy_context()  # what LangGraph does when it runs a node in a thread
    worker = threading.Thread(target=lambda: context.run(work))
    worker.start()
    worker.join()
    assert timings["rerank"] >= 0.009


def test_format_puts_the_total_first_then_the_biggest_steps():
    text = format_timings({"embed": 0.5, "total": 4.0, "generate": 2.5, "qdrant": 0.1})
    assert text == "total=4.00s generate=2.50s embed=0.50s qdrant=0.10s"


# --- reasoning effort is opt-in -------------------------------------------------------------------


def _settings(monkeypatch, effort):
    monkeypatch.setattr("app.llm.params.get_settings", lambda: SimpleNamespace(GROQ_REASONING_EFFORT=effort))


def test_nothing_is_sent_unless_the_effort_is_configured(monkeypatch):
    _settings(monkeypatch, None)
    assert reasoning_params("openai/gpt-oss-120b") == {}


def test_the_effort_goes_only_to_models_that_take_it(monkeypatch):
    _settings(monkeypatch, "low")
    assert reasoning_params("openai/gpt-oss-120b") == {"reasoning_effort": "low"}
    assert reasoning_params("llama-3.3-70b-versatile") == {}
    assert reasoning_params(None) == {}


# --- the LLM re-rank --------------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


def _fake_groq(monkeypatch, *, content, finish_reason="stop", effort=None):
    seen: dict = {}

    class Completions:
        def create(self, **kwargs):
            seen.update(kwargs)
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr("app.rag.generation._get_client", lambda: client)
    monkeypatch.setattr(
        "app.rag.retrieval.reranker.get_settings",
        lambda: SimpleNamespace(RERANKER_GROQ_MODEL="openai/gpt-oss-120b", GROQ_MODEL="openai/gpt-oss-120b"),
    )
    _settings(monkeypatch, effort)
    recorder = _Recorder()
    monkeypatch.setattr("app.rag.retrieval.reranker.logger", recorder)
    return seen, recorder


def _candidates():
    return [SimpleNamespace(text=f"listing {n}", combined_score=1.0 - n / 10) for n in (1, 2, 3)]


def test_rerank_follows_the_model_and_sends_the_effort_only_when_configured(monkeypatch):
    from app.rag.retrieval.reranker import _groq_scores

    seen, _ = _fake_groq(monkeypatch, content='{"ranking": [3, 1, 2]}')
    scores = _groq_scores("villa", _candidates())
    assert scores[2] > scores[0] > scores[1]  # passage 3 first, then 1, then 2
    assert "reasoning_effort" not in seen

    seen, _ = _fake_groq(monkeypatch, content='{"ranking": [3, 1, 2]}', effort="low")
    _groq_scores("villa", _candidates())
    assert seen["reasoning_effort"] == "low"


def test_a_ranking_cut_by_max_tokens_keeps_the_unranked_order_and_says_so(monkeypatch):
    from app.rag.retrieval.reranker import _groq_scores

    _, recorder = _fake_groq(monkeypatch, content='{"ranking": [3, ', finish_reason="length")
    candidates = _candidates()
    assert _groq_scores("villa", candidates) == [c.combined_score for c in candidates]
    assert any("max_tokens" in warning for warning in recorder.warnings)


# --- the vector search stages ------------------------------------------------------------------------


def test_the_vector_search_reports_embedding_and_qdrant_time_separately(monkeypatch):
    from app.rag.retrieval import hybrid_search as hs

    def slow_embed(query):
        time.sleep(0.01)
        return [0.1, 0.2]

    class FakeClient:
        def search(self, **kwargs):
            assert kwargs["query_vector"] == [0.1, 0.2] and kwargs["limit"] == 3
            time.sleep(0.01)
            return [SimpleNamespace(payload={"property_id": "p1", "text": "villa"}, score=0.9)]

    monkeypatch.setattr("app.rag.retrieval.hybrid_search.embed_query", slow_embed)
    timings = start_turn()
    results = hs._qdrant_search("villa", None, "properties", FakeClient(), top_k=3)
    assert [r.property_id for r in results] == ["p1"]
    assert timings["embed"] >= 0.009 and timings["qdrant"] >= 0.009


# --- the conversation service ----------------------------------------------------------------------


class _FakeGraph:
    def invoke(self, turn_input, config):
        with stage("embed"):
            time.sleep(0.01)
        with stage("generate"):
            time.sleep(0.01)
        time.sleep(0.005)
        return {"intent": "property_search", "active_agent": "property", "response": "ok", "language": "en"}


def _service(monkeypatch, backend, durable=True):
    from app.agents.service import ConversationService

    entered: list[str] = []

    @contextmanager
    def fake_thread_lock(thread_id):
        entered.append(thread_id)
        yield

    monkeypatch.setattr("app.agents.service.thread_lock", fake_thread_lock)
    monkeypatch.setattr("app.agents.service.prune_thread", lambda thread_id: time.sleep(0.002))
    monkeypatch.setattr("app.agents.service.get_settings", lambda: SimpleNamespace(TURN_LOCK_BACKEND=backend))
    monkeypatch.setattr(
        "app.agents.service.crm_service",
        SimpleNamespace(sync_conversation=lambda *a, **k: None, record_interaction=lambda **k: None),
    )
    service = ConversationService.__new__(ConversationService)  # skip building the real graph
    service._durable, service._graph = durable, _FakeGraph()
    service._locks, service._locks_guard = {}, threading.Lock()
    return service, entered


def _turn(service):
    return service.run_turn("t1", "villa in Sheikh Zayed", channel="website", customer_id="c1")


def test_the_database_turn_lock_is_off_by_default_and_on_when_asked(monkeypatch):
    service, entered = _service(monkeypatch, "process")
    _turn(service)
    assert entered == []  # one process: the in-process lock is enough, no pooled connection is held

    service, entered = _service(monkeypatch, "postgres")
    _turn(service)
    assert entered == ["t1"]

    service, entered = _service(monkeypatch, "postgres", durable=False)
    _turn(service)
    assert entered == []  # no database, nothing to lock


def test_a_turn_reports_where_its_time_went(monkeypatch):
    service, _ = _service(monkeypatch, "process")
    result = _turn(service)
    assert set(result.timings) == {"lock_wait", "embed", "generate", "graph_other", "prune", "crm_sync", "interaction", "total"}
    assert result.timings["total"] >= result.timings["embed"] + result.timings["generate"]
    assert result.timings["graph_other"] >= 0 and "graph" not in result.timings
    assert "timings" not in result.to_dict()  # internal: never sent to clients


def test_timings_do_not_carry_over_to_the_next_turn(monkeypatch):
    service, _ = _service(monkeypatch, "process")
    first, second = _turn(service), _turn(service)
    assert second.timings["embed"] < first.timings["embed"] * 1.9 + 0.02
