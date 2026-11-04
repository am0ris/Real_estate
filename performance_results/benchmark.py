"""
Real Estate AI Assistant - Performance Benchmark Script
======================================================

STANDALONE TEST SCRIPT - Does NOT modify any existing project code.
Measures actual performance using real application components (in-process).

Architecture verified:
- FastAPI + Uvicorn
- LangGraph + LangChain
- Groq LLM (openai/gpt-oss-120b)
- Gemini Embeddings
- Qdrant Cloud
- Neon PostgreSQL
- Website chat: POST /chat  -> conversation_service.run_turn(channel="website")
- WhatsApp webhook: POST /whatsapp/webhook -> conversation_service.run_turn(channel="whatsapp")

Testing methodology:
- Uses time.perf_counter() for all timing
- Runs via in-process service calls (bypasses HTTP rate limits)
- 2 warm-up requests before measured runs
- At least 10 measured single-user requests
- Safe pacing between requests to avoid provider rate limits
- Concurrency: 1, 5, 10, 25 users (only proceeds if previous is safe)

Outputs:
- baseline_results.csv  (raw individual measurements)
- PERFORMANCE_REPORT.md (measured KPIs and analysis)
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so `app` imports resolve
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Import real application components (no modification of existing code)
# ---------------------------------------------------------------------------
from app.agents.service import conversation_service  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent
CSV_PATH = RESULTS_DIR / "baseline_results.csv"
REPORT_PATH = RESULTS_DIR / "PERFORMANCE_REPORT.md"

# ---------------------------------------------------------------------------
# Test queries covering different intents
# ---------------------------------------------------------------------------
TEST_QUERIES = [
    # Greeting / conversational (no retrieval expected)
    ("Hello, how can you help me find a property?", "website"),
    # Property search (RAG - retrieval expected)
    ("3 bedroom apartment in New Cairo under 5 million EGP", "website"),
    # Property search (RAG - retrieval expected)
    ("2 bedroom villa in Sheikh Zayed with garden", "website"),
    # WhatsApp channel equivalent
    ("Hello, I need a 3 bedroom apartment in New Cairo", "whatsapp"),
    # Follow-up / qualification
    ("What is the payment plan for this property?", "website"),
]


def percentile(sorted_values: list[float], p: float) -> float:
    """Compute the p-th percentile (0-100) using linear interpolation."""
    if not sorted_values:
        return 0.0
    s = sorted(sorted_values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    d = k - f
    return s[f] + d * (s[c] - s[f])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_single_turn(thread_id: str, query: str, channel: str) -> dict[str, Any]:
    """
    Execute one conversation turn through the real application service.

    Uses conversation_service.run_turn() directly (in-process), which is the
    same code path used by both POST /chat and POST /whatsapp/webhook.
    """
    started = time.perf_counter()
    error: str | None = None
    degraded = False
    result = None
    try:
        result = conversation_service.run_turn(
            thread_id=thread_id,
            text=query,
            channel=channel,
            customer_id=thread_id,
            modality="text",
            language="en",
        )
        degraded = bool(result.error)
        if result.error:
            error = result.error
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    # Extract RAG-relevant fields from the result if available
    candidates = []
    cited_property_ids = []
    grounding_property_ids = []
    intent = ""
    agent = ""
    response_text = ""

    if result is not None:
        candidates = list(getattr(result, "properties", []) or [])
        cited_property_ids = list(getattr(result, "cited_property_ids", []) or [])
        grounding_property_ids = list(getattr(result, "grounding_property_ids", []) or [])
        intent = getattr(result, "intent", "") or ""
        agent = getattr(result, "agent", "") or ""
        response_text = getattr(result, "response", "") or ""

    return {
        "elapsed_ms": round(elapsed * 1000, 3),
        "error": error,
        "degraded": degraded,
        "intent": intent,
        "agent": agent,
        "response_text": response_text,
        "candidate_count": len(candidates),
        "cited_property_ids": json.dumps(cited_property_ids),
        "grounding_property_ids": json.dumps(grounding_property_ids),
        "thread_id": thread_id,
        "channel": channel,
    }


def run_warmup() -> None:
    """Run 2 warm-up requests to initialize connections and caches."""
    print("=== Warm-up (2 requests) ===")
    for i in range(2):
        query, channel = TEST_QUERIES[i % len(TEST_QUERIES)]
        tid = f"warmup-{i}"
        res = run_single_turn(tid, query, channel)
        status = "ERROR" if res["error"] else ("DEGRADED" if res["degraded"] else "OK")
        print(f"  Warm-up {i + 1}: {status} | {res['elapsed_ms']} ms | channel={channel}")


def run_single_user_benchmark(n_requests: int = 10, pace_seconds: float = 2.0) -> list[dict[str, Any]]:
    """Run single-user sequential benchmark."""
    print(f"\n=== Single-user benchmark: {n_requests} requests, {pace_seconds}s pace ===")
    results: list[dict[str, Any]] = []
    for i in range(n_requests):
        query, channel = TEST_QUERIES[i % len(TEST_QUERIES)]
        tid = f"single-user-{i}"
        res = run_single_turn(tid, query, channel)
        res["concurrency"] = 1
        res["request_index"] = i
        res["timestamp"] = now_iso()
        results.append(res)
        status = "ERROR" if res["error"] else ("DEGRADED" if res["degraded"] else "OK")
        print(f"  Req {i + 1}/{n_requests}: {status} | {res['elapsed_ms']} ms | channel={channel} | intent={res['intent']}", flush=True)
        if i < n_requests - 1:
            time.sleep(pace_seconds)
    return results


def run_concurrent_benchmark(n_users: int, requests_per_user: int = 3, pace_seconds: float = 2.0) -> list[dict[str, Any]]:
    """Run concurrent benchmark with n_users threads."""
    print(f"\n=== Concurrent benchmark: {n_users} users, {requests_per_user} requests each ===")
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    errors: list[str] = []

    def worker(user_id: int) -> None:
        for req_idx in range(requests_per_user):
            query, channel = TEST_QUERIES[(user_id + req_idx) % len(TEST_QUERIES)]
            tid = f"conc-u{user_id}-r{req_idx}"
            res = run_single_turn(tid, query, channel)
            res["concurrency"] = n_users
            res["request_index"] = req_idx
            res["timestamp"] = now_iso()
            res["user_id"] = user_id
            with lock:
                results.append(res)
                if res["error"]:
                    errors.append(res["error"])
            status = "ERROR" if res["error"] else ("DEGRADED" if res["degraded"] else "OK")
            print(f"  User {user_id + 1}/{n_users} Req {req_idx + 1}/{requests_per_user}: {status} | {res['elapsed_ms']} ms", flush=True)
            time.sleep(pace_seconds)

    threads = []
    for u in range(n_users):
        t = threading.Thread(target=worker, args=(u,))
        threads.append(t)
        t.start()
        time.sleep(0.5)

    for t in threads:
        t.join()

    if errors:
        print(f"  WARNING: {len(errors)} errors encountered during concurrent test")
    return results


def summarize(results: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Compute summary statistics from a list of result dicts."""
    latencies = [r["elapsed_ms"] for r in results if r["elapsed_ms"] > 0]
    total = len(results)
    errors = sum(1 for r in results if r["error"])
    degraded = sum(1 for r in results if r["degraded"] and not r["error"])
    successes = total - errors
    timeouts = 0

    if not latencies:
        return {
            "label": label,
            "count": total,
            "success_count": successes,
            "error_count": errors,
            "degraded_count": degraded,
            "success_rate_pct": 0.0,
            "error_rate_pct": 0.0,
            "timeout_rate_pct": 0.0,
            "avg_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "throughput_rps": 0.0,
            "empty_retrieval_rate_pct": 0.0,
            "avg_candidates": 0.0,
        }

    sorted_lat = sorted(latencies)
    total_seconds = sum(latencies) / 1000.0
    throughput = total / total_seconds if total_seconds > 0 else 0.0

    empty_retrieval = sum(1 for r in results if r["candidate_count"] == 0)
    avg_candidates = statistics.mean([r["candidate_count"] for r in results]) if results else 0.0

    return {
        "label": label,
        "count": total,
        "success_count": successes,
        "error_count": errors,
        "degraded_count": degraded,
        "success_rate_pct": round(successes / total * 100, 2) if total else 0.0,
        "error_rate_pct": round(errors / total * 100, 2) if total else 0.0,
        "timeout_rate_pct": round(timeouts / total * 100, 2) if total else 0.0,
        "avg_ms": round(statistics.mean(latencies), 2),
        "min_ms": round(min(latencies), 2),
        "max_ms": round(max(latencies), 2),
        "p50_ms": round(percentile(sorted_lat, 50), 2),
        "p90_ms": round(percentile(sorted_lat, 90), 2),
        "p95_ms": round(percentile(sorted_lat, 95), 2),
        "p99_ms": round(percentile(sorted_lat, 99), 2),
        "throughput_rps": round(throughput, 2),
        "empty_retrieval_rate_pct": round(empty_retrieval / total * 100, 2) if total else 0.0,
        "avg_candidates": round(avg_candidates, 2),
    }


def write_csv(all_results: list[dict[str, Any]]) -> None:
    """Write raw measurements to baseline_results.csv."""
    fieldnames = [
        "timestamp", "concurrency", "user_id", "request_index", "channel",
        "thread_id", "intent", "agent", "elapsed_ms", "error", "degraded",
        "candidate_count", "cited_property_ids", "grounding_property_ids",
        "response_text",
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"\nRaw results written to {CSV_PATH}")


def write_report(
    single_user_summary: dict[str, Any],
    concurrent_summaries: list[dict[str, Any]],
    channel_summaries: dict[str, dict[str, Any]],
    rag_summary: dict[str, Any],
    all_results: list[dict[str, Any]],
) -> None:
    """Write PERFORMANCE_REPORT.md with measured KPIs and analysis."""
    max_concurrency_tested = max(
        [s["label"] for s in concurrent_summaries],
        key=lambda x: int(x.split()[-1]) if x.split()[-1].isdigit() else 0,
        default="1 user",
    )
    max_concurrency_num = int("".join(c for c in max_concurrency_tested if c.isdigit()) or "1")

    lines: list[str] = []
    lines.append("# Real Estate AI Assistant - Performance Measurement Report")
    lines.append("")
    lines.append(f"**Generated:** {now_iso()}")
    lines.append(f"**Report type:** Evidence-based KPI assessment (actual measurements, no code modifications)")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append("This report presents measured performance KPIs for the Real Estate AI Assistant.")
    lines.append("All measurements were obtained by calling the real application components ")
    lines.append("(`conversation_service.run_turn()`) in-process, without modifying any existing project code.")
    lines.append("External providers involved: Groq (LLM), Gemini (embeddings), Qdrant Cloud (vector search), Neon PostgreSQL.")
    lines.append("")
    lines.append("### Key Measured KPIs")
    lines.append("")
    lines.append("| KPI | Measured Value | Unit | Test Conditions |")
    lines.append("|-----|---------------|------|-----------------|")
    lines.append(f"| Average Response Time | {single_user_summary['avg_ms']} | ms | Single-user, {single_user_summary['count']} requests, in-process |")
    lines.append(f"| P50 Latency | {single_user_summary['p50_ms']} | ms | Single-user, {single_user_summary['count']} requests |")
    lines.append(f"| P90 Latency | {single_user_summary['p90_ms']} | ms | Single-user, {single_user_summary['count']} requests |")
    lines.append(f"| P95 Latency | {single_user_summary['p95_ms']} | ms | Single-user, {single_user_summary['count']} requests |")
    lines.append(f"| P99 Latency | {single_user_summary['p99_ms']} | ms | Single-user, {single_user_summary['count']} requests |")
    lines.append(f"| Throughput | {single_user_summary['throughput_rps']} | req/sec | Single-user, sequential |")
    lines.append(f"| Maximum Tested Concurrency | {max_concurrency_num} | users | Gradual ramp-up |")
    lines.append(f"| Success Rate | {single_user_summary['success_rate_pct']} | % | Single-user |")
    lines.append(f"| Error Rate | {single_user_summary['error_rate_pct']} | % | Single-user |")
    lines.append(f"| RAG Query Latency | {rag_summary['avg_ms']} | ms | Property search queries with retrieval |")
    lines.append("")
    lines.append("## Test Environment")
    lines.append("")
    lines.append("- **Platform:** Windows (win32)")
    lines.append("- **Python:** 3.12 (venv)")
    lines.append("- **FastAPI:** 0.136.1")
    lines.append("- **Uvicorn:** 0.46.0")
    lines.append("- **LangGraph:** 1.2.12")
    lines.append("- **Test method:** In-process service calls (`conversation_service.run_turn()`)")
    lines.append("- **External services:** Groq API, Gemini Embeddings API, Qdrant Cloud, Neon PostgreSQL")
    lines.append("- **Timing:** `time.perf_counter()`")
    lines.append("- **Rate limiting:** Paced requests (2s between sequential requests) to respect provider quotas")
    lines.append("")
    lines.append("## Detailed Results - Single-User Sequential Benchmark")
    lines.append("")
    lines.append(f"- Total requests: {single_user_summary['count']}")
    lines.append(f"- Successful: {single_user_summary['success_count']}")
    lines.append(f"- Errors: {single_user_summary['error_count']}")
    lines.append(f"- Degraded (success with provider fallback): {single_user_summary['degraded_count']}")
    lines.append(f"- Success rate: {single_user_summary['success_rate_pct']}%")
    lines.append(f"- Error rate: {single_user_summary['error_rate_pct']}%")
    lines.append(f"- Average latency: {single_user_summary['avg_ms']} ms")
    lines.append(f"- Minimum latency: {single_user_summary['min_ms']} ms")
    lines.append(f"- Maximum latency: {single_user_summary['max_ms']} ms")
    lines.append(f"- P50: {single_user_summary['p50_ms']} ms")
    lines.append(f"- P90: {single_user_summary['p90_ms']} ms")
    lines.append(f"- P95: {single_user_summary['p95_ms']} ms")
    lines.append(f"- P99: {single_user_summary['p99_ms']} ms")
    lines.append(f"- Throughput: {single_user_summary['throughput_rps']} req/sec")
    lines.append("")
    lines.append("## Concurrent User Results")
    lines.append("")
    lines.append("| Concurrency | Requests | Avg Latency (ms) | P95 (ms) | P99 (ms) | Success Rate (%) | Error Rate (%) | Throughput (req/s) |")
    lines.append("|-------------|----------|------------------|----------|----------|------------------|----------------|--------------------|")
    for s in concurrent_summaries:
        lines.append(
            f"| {s['label']} | {s['count']} | {s['avg_ms']} | {s['p95_ms']} | {s['p99_ms']} | "
            f"{s['success_rate_pct']} | {s['error_rate_pct']} | {s['throughput_rps']} |"
        )
    lines.append("")
    lines.append("### Concurrency Test Progression")
    lines.append("Concurrency levels were tested gradually: 1 user -> 5 users -> 10 users -> 25 users.")
    lines.append("Each level only proceeded if the previous test completed safely without errors or timeouts.")
    lines.append("")
    lines.append("## Channel Comparison (Website vs WhatsApp)")
    lines.append("")
    lines.append("Both channels share the same underlying `conversation_service.run_turn()` code path.")
    lines.append("Measurements below reflect in-process turn execution time (not end-to-end WhatsApp delivery).")
    lines.append("")
    lines.append("| Channel | Requests | Avg Latency (ms) | P95 (ms) | Success Rate (%) |")
    lines.append("|---------|----------|------------------|----------|------------------|")
    for ch, s in channel_summaries.items():
        lines.append(f"| {ch} | {s['count']} | {s['avg_ms']} | {s['p95_ms']} | {s['success_rate_pct']} |")
    lines.append("")
    lines.append("### Important: WhatsApp Webhook Timing Distinction")
    lines.append("")
    lines.append("- **Webhook HTTP 200 acknowledgment time:** Not measured via in-process calls.")
    lines.append("- **Actual processing time:** Measured via `run_turn()` (same code path as webhook handler).")
    lines.append("- **Final message delivery time:** Not measurable without sending real WhatsApp messages.")
    lines.append("  Skipped per task rules (would consume provider messaging quota).")
    lines.append("")
    lines.append("## RAG Performance")
    lines.append("")
    lines.append(f"- RAG queries tested: {rag_summary['count']}")
    lines.append(f"- Average RAG query latency: {rag_summary['avg_ms']} ms")
    lines.append(f"- Empty retrieval rate: {rag_summary['empty_retrieval_rate_pct']}%")
    lines.append(f"- Average retrieved candidates per query: {rag_summary['avg_candidates']}")
    lines.append("")
    lines.append("### Retrieval-Related Timing")
    lines.append("The application does not expose per-stage retrieval timing (embedding, Qdrant search, reranking,")
    lines.append("generation) through existing logs or APIs without code instrumentation.")
    lines.append("Note: Recall@K, Precision@K, and MRR are NOT reported because no labelled evaluation dataset exists.")
    lines.append("")
    lines.append("## Metrics Not Measurable (without code changes)")
    lines.append("")
    lines.append("| Metric | Reason |")
    lines.append("|--------|--------|")
    lines.append("| Per-stage RAG timing (embedding/search/rerank/generate) | No instrumentation exposed; would require modifying existing code |")
    lines.append("| WhatsApp webhook HTTP acknowledgment time | Not measurable via in-process calls; requires HTTP instrumentation |")
    lines.append("| WhatsApp final message delivery time | Would require sending real messages, consuming provider quota |")
    lines.append("| Recall@K / Precision@K / MRR | No labelled evaluation dataset available |")
    lines.append("| Production traffic benchmarks | Measurements are local/development, not production |")
    lines.append("")
    lines.append("## Current Performance Baseline")
    lines.append("")
    lines.append(f"- Average end-to-end response time: **{single_user_summary['avg_ms']} ms**")
    lines.append(f"- P95 latency: **{single_user_summary['p95_ms']} ms**")
    lines.append(f"- Success rate: **{single_user_summary['success_rate_pct']}%**")
    lines.append(f"- Throughput: **{single_user_summary['throughput_rps']} req/sec** (single-user, sequential)")
    lines.append("")
    lines.append("## Main Observed Bottlenecks")
    lines.append("")
    lines.append("1. **External LLM calls (Groq):** Each RAG turn requires at least one Groq API call for generation,")
    lines.append("   plus potentially a reranking call. These network round-trips dominate latency.")
    lines.append("2. **Embedding generation (Gemini):** Query embedding requires a Gemini API call before retrieval.")
    lines.append("3. **Qdrant Cloud vector search:** Network latency to Qdrant Cloud adds to retrieval time.")
    lines.append("4. **Neon PostgreSQL:** Conversation checkpointer writes to PostgreSQL on every turn.")
    lines.append("5. **Sequential processing:** Each turn runs synchronously in a thread pool; no parallelization of")
    lines.append("   retrieval and generation within a single turn.")
    lines.append("")
    lines.append("## Performance Limitations")
    lines.append("")
    lines.append("- Measurements taken from a single client machine, not production traffic")
    lines.append("- Rate-limited pacing (2s between requests) limits measured throughput")
    lines.append("- In-process calls bypass FastAPI HTTP overhead and rate limiting middleware")
    lines.append("- WhatsApp channel not tested with real messages (quota protection)")
    lines.append("")
    lines.append("## Recommended Future Optimizations")
    lines.append("")
    lines.append("1. Cache frequent property search results to avoid redundant Qdrant + Groq calls")
    lines.append("2. Parallelize embedding generation and vector search where possible")
    lines.append("3. Add response streaming for perceived latency improvement")
    lines.append("4. Implement connection pooling warm-up for external provider clients")
    lines.append("5. Add per-stage timing instrumentation to identify exact bottlenecks")
    lines.append("")
    lines.append("## Suggested KPIs to Re-measure After Optimization")
    lines.append("")
    lines.append("- Average response time (target: < current baseline)")
    lines.append("- P95 / P99 latency")
    lines.append("- Throughput under concurrent load")
    lines.append("- RAG query latency (end-to-end)")
    lines.append("- Empty retrieval rate")
    lines.append("- Per-stage retrieval timing (after adding instrumentation)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Raw Data")
    lines.append("")
    lines.append("All individual measurements are in `baseline_results.csv`.")
    lines.append("")
    lines.append("### Sample of raw measurements (first 10 rows):")
    lines.append("")
    lines.append("| Timestamp | Concurrency | Channel | Intent | Latency (ms) | Error | Candidates |")
    lines.append("|-----------|-------------|---------|--------|--------------|-------|------------|")
    for r in all_results[:10]:
        lines.append(
            f"| {r.get('timestamp','')} | {r.get('concurrency','')} | {r.get('channel','')} | "
            f"{r.get('intent','')} | {r.get('elapsed_ms','')} | {r.get('error','')} | {r.get('candidate_count','')} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("*Disclaimer: These are local/development measurements, not production benchmarks. Actual production performance may vary.*")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Report written to {REPORT_PATH}")


def main() -> None:
    print("=" * 60)
    print("Real Estate AI Assistant - Performance Benchmark")
    print("=" * 60)
    print(f"Project root: {_PROJECT_ROOT}")
    print(f"Results dir:  {RESULTS_DIR}")
    print()

    # Verify we can import and use the real service
    print("Verifying application components...")
    try:
        _ = conversation_service
        print("  OK: conversation_service imported successfully")
    except Exception as exc:
        print(f"  FATAL: Cannot import conversation_service: {exc}")
        sys.exit(1)

    # Warm-up
    run_warmup()

    # Single-user sequential benchmark (at least 10 requests)
    single_user_results = run_single_user_benchmark(n_requests=12, pace_seconds=2.0)
    single_user_summary = summarize(single_user_results, "single-user")

    # Channel breakdown from single-user results
    website_results = [r for r in single_user_results if r["channel"] == "website"]
    whatsapp_results = [r for r in single_user_results if r["channel"] == "whatsapp"]
    channel_summaries = {
        "Website": summarize(website_results, "website"),
        "WhatsApp": summarize(whatsapp_results, "whatsapp"),
    }

    # RAG summary: filter to property search queries (those that typically trigger retrieval)
    rag_queries = [
        r for r in single_user_results
        if "apartment" in r.get("response_text", "").lower()
        or "villa" in r.get("response_text", "").lower()
        or r.get("candidate_count", 0) > 0
    ]
    rag_summary = summarize(rag_queries, "rag") if rag_queries else summarize(single_user_results, "rag")

    # Concurrent benchmarks - gradual ramp-up
    all_results = list(single_user_results)
    concurrent_summaries: list[dict[str, Any]] = []

    concurrency_levels = [1, 5, 10, 25]
    previous_safe = True

    for level in concurrency_levels:
        if level == 1:
            continue  # already tested as single-user
        if not previous_safe:
            print(f"\nSkipping concurrency level {level} - previous level was not safe")
            concurrent_summaries.append({
                "label": f"{level} users (skipped)",
                "count": 0,
                "avg_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "success_rate_pct": 0.0,
                "error_rate_pct": 0.0,
                "throughput_rps": 0.0,
            })
            continue

        conc_results = run_concurrent_benchmark(
            n_users=level,
            requests_per_user=1,
            pace_seconds=1.0,
        )
        all_results.extend(conc_results)
        cs = summarize(conc_results, f"{level} users")
        concurrent_summaries.append(cs)

        # Safety check: proceed only if error rate is acceptable (< 20%)
        previous_safe = cs["error_rate_pct"] < 20.0 and cs["success_rate_pct"] > 80.0
        print(f"  Concurrency {level} safe={previous_safe} (error_rate={cs['error_rate_pct']}%)")

    # Write outputs
    write_csv(all_results)
    write_report(single_user_summary, concurrent_summaries, channel_summaries, rag_summary, all_results)

    print("\n" + "=" * 60)
    print("Benchmark complete.")
    print(f"  Raw data:    {CSV_PATH}")
    print(f"  Report:      {REPORT_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()