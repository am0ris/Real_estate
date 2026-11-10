#!/usr/bin/env python3
"""
probe_latency.py - FREE diagnostics (no Groq/Gemini calls) for the per-turn latency floor and the pool timeouts.

It measures, from the machine that runs the benchmark:
  1. time to open a NEW PostgreSQL connection (what the pool pays when min_size=0 or a connection went stale)
  2. round-trip time of 'SELECT 1' on a warm connection (what every statement of a turn pays)
  3. cost of an advisory transaction lock (the per-conversation lock every turn holds for its whole duration)
  4. time to open N connections at once (does the server accept 4/6/10 parallel connections quickly?)
  5. optional: Qdrant round trip

Put it in scripts/ and run from the project root:
  python scripts/probe_latency.py                       # uses the app's configured database
  python scripts/probe_latency.py --dsn "postgresql://u:p@127.0.0.1:55432/db"   # e.g. a local Docker Postgres
  python scripts/probe_latency.py --qdrant              # also time Qdrant (free read)
Compare the Neon numbers with a local Postgres: if local is ~1 ms and Neon is ~100+ ms, the database
round trips (not the LLM) are the latency floor.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pct(v: list[float], p: float) -> float:
    s = sorted(v)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def stats(label: str, ms: list[float]) -> dict:
    row = {"label": label, "n": len(ms), "min": min(ms), "median": statistics.median(ms), "p95": pct(ms, 95), "max": max(ms)}
    print(f"  {label:<46} n={row['n']:<3} min {row['min']:8.1f} | median {row['median']:8.1f} | p95 {row['p95']:8.1f} | max {row['max']:8.1f}  (ms)")
    return row


def timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def probe_db(connect, rounds_connect: int, rounds_query: int, parallel: list[int]) -> dict:
    out: dict = {}
    print("\nPostgreSQL")
    # 1. new connection time
    conn_ms = []
    for _ in range(rounds_connect):
        box = {}
        conn_ms.append(timed(lambda: box.setdefault("c", connect())))
        box["c"].close()
        time.sleep(0.2)
    out["connect"] = stats("open a NEW connection", conn_ms)
    # 2/3. warm statements
    conn = connect()
    try:
        conn.execute("SELECT 1")
        q = [timed(lambda: conn.execute("SELECT 1").fetchone()) for _ in range(rounds_query)]
        out["select1"] = stats("SELECT 1 on a warm connection (1 round trip)", q)

        def locked() -> None:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (987654321,))
        lk = [timed(locked) for _ in range(10)]
        out["lock"] = stats("transaction + advisory lock (3 round trips)", lk)
        seq = timed(lambda: [conn.execute("SELECT 1").fetchone() for _ in range(60)])
        out["sixty"] = {"total_ms": seq}
        print(f"  60 sequential statements (a rough upper bound for one turn's DB work): {seq:.0f} ms total")
    finally:
        conn.close()
    # 4. parallel connections
    for n in parallel:
        times: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            t = time.perf_counter()
            try:
                c = connect()
                with lock:
                    times.append((time.perf_counter() - t) * 1000)
                time.sleep(0.5)
                c.close()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    times.append(float("inf"))
                print(f"    connection failed: {type(exc).__name__}: {exc}")

        ts = [threading.Thread(target=worker) for _ in range(n)]
        t0 = time.perf_counter()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ok = [x for x in times if x != float("inf")]
        if ok:
            print(f"  open {n:>2} connections at once: slowest {max(ok):8.1f} ms, median {statistics.median(ok):8.1f} ms, "
                  f"failed {len(times) - len(ok)}, wall {1000 * (time.perf_counter() - t0):.0f} ms")
        out[f"parallel_{n}"] = {"ok": len(ok), "failed": len(times) - len(ok)}
    return out


def interpret(res: dict) -> None:
    print("\nReading the numbers")
    s1 = res["select1"]["median"]
    sixty = res["sixty"]["total_ms"]
    conn = res["connect"]["median"]
    print(f"  - One database round trip costs about {s1:.0f} ms; 60 of them cost about {sixty / 1000:.1f} s.")
    if s1 > 50:
        print("    A turn that writes several checkpoints, locks and prunes can spend seconds here: the database distance/latency,")
        print("    not the LLM, is a likely part of the per-turn floor. Compare with a local Postgres (--dsn).")
    else:
        print("    Round trips are cheap, so the per-turn floor is probably somewhere else (compute, model clients, many sequential steps).")
    print(f"  - Opening a new connection takes about {conn:.0f} ms. With min_size=0 and a per-checkout health check, every")
    print("    cold or stale checkout pays this (a suspended Neon compute can add much more on the first request).")
    print("  - A turn holds one of the 4 turn-lock connections for its whole duration; if turns last longer than the 20 s pool")
    print("    timeout, the 5th concurrent turn fails with PoolTimeout.")


def probe_qdrant(rounds: int) -> None:
    print("\nQdrant")
    from app.database.vector_db import create_client  # type: ignore
    from app.helpers.config import get_settings  # type: ignore

    client = create_client()
    name = get_settings().QDRANT_COLLECTION
    try:
        client.get_collection(name)
        ms = [timed(lambda: client.get_collection(name)) for _ in range(rounds)]
        stats("get_collection round trip", ms)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Free latency probe (database and optionally Qdrant)")
    ap.add_argument("--dsn", help="libpq connection string; default: the app's configured database")
    ap.add_argument("--connect-rounds", type=int, default=5)
    ap.add_argument("--query-rounds", type=int, default=30)
    ap.add_argument("--parallel", default="4,6,10")
    ap.add_argument("--qdrant", action="store_true")
    args = ap.parse_args()
    dsn = args.dsn
    if not dsn:
        from app.database.url import libpq_conninfo  # type: ignore
        dsn = libpq_conninfo()
    import psycopg  # type: ignore

    def connect():
        return psycopg.connect(dsn, autocommit=True, connect_timeout=10)

    host = psycopg.conninfo.conninfo_to_dict(dsn).get("host", "?")
    print(f"Target host: {host}")
    res = probe_db(connect, args.connect_rounds, args.query_rounds, [int(x) for x in args.parallel.split(",") if x.strip()])
    interpret(res)
    if args.qdrant:
        probe_qdrant(10)


if __name__ == "__main__":
    main()
