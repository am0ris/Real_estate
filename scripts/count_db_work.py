#!/usr/bin/env python3
"""
count_db_work.py - how many database round trips does ONE conversation turn make?  (free for 'reset' turns)

The probe showed one round trip costs ~152 ms to Neon (0.9 ms to a local Postgres). Multiply that by the number
of statements a turn executes and you get the database share of the turn latency. This script counts them.

Method: the pool connections use autocommit, so every statement is its own transaction. We read PostgreSQL's
own counter (xact_commit + xact_rollback) before and after one turn and subtract the cost of the reads themselves.
Run it against a LOCAL Postgres (fast, so the turn finishes quickly and nothing else disturbs the counter):

  docker run -d --name aqar-test-pg -e POSTGRES_USER=aqar -e POSTGRES_PASSWORD=aqar_test_pw -e POSTGRES_DB=aqar_test -p 127.0.0.1:55432:5432 postgres:16-alpine
  $env:DATABASE_URL = "postgresql://aqar:aqar_test_pw@127.0.0.1:55432/aqar_test"
  python scripts/migrate.py
  python scripts/count_db_work.py --dsn $env:DATABASE_URL            # 'start over' turns: no provider calls
  python scripts/count_db_work.py --dsn $env:DATABASE_URL --search   # also ONE real search turn (1 provider-backed turn)

Do not run other traffic against that database while it measures.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SETTLE_S = 2.0  # PostgreSQL flushes its statistics counters a moment after a transaction ends


def measure(read_counter, run_turn, runs: int, settle: float = SETTLE_S) -> list[int]:
    """Return, for each run, the number of transactions (~statements) the turn caused."""
    results: list[int] = []
    for i in range(runs):
        time.sleep(settle)
        a = read_counter()
        time.sleep(settle)
        b = read_counter()
        overhead = b - a            # what one read of the counter costs by itself
        run_turn(i)
        time.sleep(settle)
        c = read_counter()
        results.append(max(0, c - b - overhead))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Count database round trips per conversation turn (local Postgres recommended)")
    ap.add_argument("--dsn", required=True, help="libpq/URL of the database the app should use (local Docker recommended)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--search", action="store_true", help="also measure one real search turn (uses Groq/Gemini once)")
    ap.add_argument("--rtt-ms", type=float, default=152.0, help="round trip to the database you care about (from probe_latency.py)")
    args = ap.parse_args()

    os.environ["DATABASE_URL"] = args.dsn  # must be set before the app reads its settings
    import psycopg  # type: ignore
    from app.agents.service import conversation_service  # type: ignore

    conn = psycopg.connect(args.dsn, autocommit=True)

    def read_counter() -> int:
        row = conn.execute("SELECT xact_commit + xact_rollback FROM pg_stat_database WHERE datname = current_database()").fetchone()
        return int(row[0])

    def reset_turn(i: int) -> None:
        conversation_service.run_turn(thread_id=f"count-reset-{time.time()}-{i}", text="ابدأ من جديد", channel="website",
                                      customer_id="count", modality="text", language="ar")

    def search_turn(i: int) -> None:
        conversation_service.run_turn(thread_id=f"count-search-{time.time()}-{i}", text="villa in Sheikh Zayed",
                                      channel="website", customer_id="count", modality="text", language="en")

    print("Warm-up turn (creates tables/clients; not counted)...")
    reset_turn(-1)
    for label, fn, runs in [("'start over' turn (no provider calls)", reset_turn, args.runs)] + \
                           ([("search turn (1 Gemini embedding + Groq call(s))", search_turn, 1)] if args.search else []):
        counts = measure(read_counter, fn, runs)
        med = statistics.median(counts)
        print(f"\n{label}: transactions per turn = {counts}  (median {med:.0f})")
        print(f"  At {args.rtt_ms:.0f} ms per round trip this is about {med * args.rtt_ms / 1000:.1f} s of database time per turn,")
        print(f"  and at 1 ms (database next to the app) about {med / 1000:.2f} s.")
    print("\nNote: counts are transactions; with autocommit each statement is one transaction, so this is the number of")
    print("round trips. Sequences such as 'BEGIN ... COMMIT' count once, so the true statement count can be slightly higher.")
    conn.close()


if __name__ == "__main__":
    main()
