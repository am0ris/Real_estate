"""Where the time of one customer turn goes.

A turn chains several slow steps: the embedding call, the vector search, the LLM re-rank, the
LLM answer and the database round trips (conversation state, locks, CRM). The caller sees only the
total, so a 12-second turn and a 19-second turn look the same. ``stage`` times each step and
``ConversationService.run_turn`` logs one line per turn with the breakdown.

The timings live in a ``ContextVar``: code that runs outside a turn (tests, scripts) simply records
nothing. The dict itself is shared, so a stage that runs in a worker thread (LangGraph copies the
context) is still counted.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_timings: ContextVar[dict[str, float] | None] = ContextVar("turn_timings", default=None)


def start_turn() -> dict[str, float]:
    """Begin collecting timings for the current turn and return the (live) dict."""
    timings: dict[str, float] = {}
    _timings.set(timings)
    return timings


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Add the time spent inside the ``with`` block to ``name`` (summed if it runs several times)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        timings = _timings.get()
        if timings is not None:
            timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def format_timings(timings: dict[str, float]) -> str:
    """``total=12.3s embed=1.2s generate=8.4s ...`` with the biggest steps first (total leads)."""
    ordered = sorted((item for item in timings.items() if item[0] != "total"), key=lambda item: -item[1])
    if "total" in timings:
        ordered.insert(0, ("total", timings["total"]))
    return " ".join(f"{name}={seconds:.2f}s" for name, seconds in ordered)
