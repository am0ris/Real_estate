# Real Estate AI Assistant - Performance Measurement Report

**Generated:** 2026-11-03T19:02:19.052221+00:00
**Report type:** Evidence-based KPI assessment (actual measurements, no code modifications)

## Executive Summary

This report presents measured performance KPIs for the Real Estate AI Assistant.
All measurements were obtained by calling the real application components 
(`conversation_service.run_turn()`) in-process, without modifying any existing project code.
External providers involved: Groq (LLM), Gemini (embeddings), Qdrant Cloud (vector search), Neon PostgreSQL.

### Key Measured KPIs

| KPI | Measured Value | Unit | Test Conditions |
|-----|---------------|------|-----------------|
| Average Response Time | 11392.67 | ms | Single-user, 12 requests, in-process |
| P50 Latency | 11111.38 | ms | Single-user, 12 requests |
| P90 Latency | 12812.08 | ms | Single-user, 12 requests |
| P95 Latency | 13242.52 | ms | Single-user, 12 requests |
| P99 Latency | 13621.07 | ms | Single-user, 12 requests |
| Throughput | 0.09 | req/sec | Single-user, sequential |
| Maximum Tested Concurrency | 5 | users | Gradual ramp-up |
| Success Rate | 100.0 | % | Single-user |
| Error Rate | 0.0 | % | Single-user |
| RAG Query Latency | 11295.45 | ms | Property search queries with retrieval |

## Test Environment

- **Platform:** Windows (win32)
- **Python:** 3.12 (venv)
- **FastAPI:** 0.136.1
- **Uvicorn:** 0.46.0
- **LangGraph:** 1.2.12
- **Test method:** In-process service calls (`conversation_service.run_turn()`)
- **External services:** Groq API, Gemini Embeddings API, Qdrant Cloud, Neon PostgreSQL
- **Timing:** `time.perf_counter()`
- **Rate limiting:** Paced requests (2s between sequential requests) to respect provider quotas

## Detailed Results - Single-User Sequential Benchmark

- Total requests: 12
- Successful: 12
- Errors: 0
- Degraded (success with provider fallback): 0
- Success rate: 100.0%
- Error rate: 0.0%
- Average latency: 11392.67 ms
- Minimum latency: 10096.41 ms
- Maximum latency: 13715.71 ms
- P50: 11111.38 ms
- P90: 12812.08 ms
- P95: 13242.52 ms
- P99: 13621.07 ms
- Throughput: 0.09 req/sec

## Concurrent User Results

| Concurrency | Requests | Avg Latency (ms) | P95 (ms) | P99 (ms) | Success Rate (%) | Error Rate (%) | Throughput (req/s) |
|-------------|----------|------------------|----------|----------|------------------|----------------|--------------------|
| 5 users | 5 | 26518.12 | 29999.49 | 30121.36 | 80.0 | 20.0 | 0.04 |
| 10 users (skipped) | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 25 users (skipped) | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

### Concurrency Test Progression
Concurrency levels were tested gradually: 1 user -> 5 users -> 10 users -> 25 users.
Each level only proceeded if the previous test completed safely without errors or timeouts.

## Channel Comparison (Website vs WhatsApp)

Both channels share the same underlying `conversation_service.run_turn()` code path.
Measurements below reflect in-process turn execution time (not end-to-end WhatsApp delivery).

| Channel | Requests | Avg Latency (ms) | P95 (ms) | Success Rate (%) |
|---------|----------|------------------|----------|------------------|
| Website | 10 | 11057.38 | 12187.34 | 100.0 |
| WhatsApp | 2 | 13069.09 | 13651.05 | 100.0 |

### Important: WhatsApp Webhook Timing Distinction

- **Webhook HTTP 200 acknowledgment time:** Not measured via in-process calls.
- **Actual processing time:** Measured via `run_turn()` (same code path as webhook handler).
- **Final message delivery time:** Not measurable without sending real WhatsApp messages.
  Skipped per task rules (would consume provider messaging quota).

## RAG Performance

- RAG queries tested: 7
- Average RAG query latency: 11295.45 ms
- Empty retrieval rate: 42.86%
- Average retrieved candidates per query: 2.86

### Retrieval-Related Timing
The application does not expose per-stage retrieval timing (embedding, Qdrant search, reranking,
generation) through existing logs or APIs without code instrumentation.
Note: Recall@K, Precision@K, and MRR are NOT reported because no labelled evaluation dataset exists.

## Metrics Not Measurable (without code changes)

| Metric | Reason |
|--------|--------|
| Per-stage RAG timing (embedding/search/rerank/generate) | No instrumentation exposed; would require modifying existing code |
| WhatsApp webhook HTTP acknowledgment time | Not measurable via in-process calls; requires HTTP instrumentation |
| WhatsApp final message delivery time | Would require sending real messages, consuming provider quota |
| Recall@K / Precision@K / MRR | No labelled evaluation dataset available |
| Production traffic benchmarks | Measurements are local/development, not production |

## Current Performance Baseline

- Average end-to-end response time: **11392.67 ms**
- P95 latency: **13242.52 ms**
- Success rate: **100.0%**
- Throughput: **0.09 req/sec** (single-user, sequential)

## Main Observed Bottlenecks

1. **External LLM calls (Groq):** Each RAG turn requires at least one Groq API call for generation,
   plus potentially a reranking call. These network round-trips dominate latency.
2. **Embedding generation (Gemini):** Query embedding requires a Gemini API call before retrieval.
3. **Qdrant Cloud vector search:** Network latency to Qdrant Cloud adds to retrieval time.
4. **Neon PostgreSQL:** Conversation checkpointer writes to PostgreSQL on every turn.
5. **Sequential processing:** Each turn runs synchronously in a thread pool; no parallelization of
   retrieval and generation within a single turn.

## Performance Limitations

- Measurements taken from a single client machine, not production traffic
- Rate-limited pacing (2s between requests) limits measured throughput
- In-process calls bypass FastAPI HTTP overhead and rate limiting middleware
- WhatsApp channel not tested with real messages (quota protection)

## Recommended Future Optimizations

1. Cache frequent property search results to avoid redundant Qdrant + Groq calls
2. Parallelize embedding generation and vector search where possible
3. Add response streaming for perceived latency improvement
4. Implement connection pooling warm-up for external provider clients
5. Add per-stage timing instrumentation to identify exact bottlenecks

## Suggested KPIs to Re-measure After Optimization

- Average response time (target: < current baseline)
- P95 / P99 latency
- Throughput under concurrent load
- RAG query latency (end-to-end)
- Empty retrieval rate
- Per-stage retrieval timing (after adding instrumentation)

---

## Raw Data

All individual measurements are in `baseline_results.csv`.

### Sample of raw measurements (first 10 rows):

| Timestamp | Concurrency | Channel | Intent | Latency (ms) | Error | Candidates |
|-----------|-------------|---------|--------|--------------|-------|------------|
| 2026-11-03T18:59:17.798129+00:00 | 1 | website | lead_qualification | 10131.441 | None | 0 |
| 2026-11-03T18:59:32.654068+00:00 | 1 | website | property_search | 12855.372 | None | 0 |
| 2026-11-03T18:59:46.025680+00:00 | 1 | website | property_search | 11370.848 | None | 0 |
| 2026-11-03T19:00:01.742553+00:00 | 1 | whatsapp | property_search | 13715.708 | None | 5 |
| 2026-11-03T19:00:14.638516+00:00 | 1 | website | payment_plan | 10895.778 | None | 5 |
| 2026-11-03T19:00:26.735514+00:00 | 1 | website | lead_qualification | 10096.409 | None | 0 |
| 2026-11-03T19:00:39.931779+00:00 | 1 | website | property_search | 11194.863 | None | 0 |
| 2026-11-03T19:00:53.047173+00:00 | 1 | website | property_search | 11114.673 | None | 0 |
| 2026-11-03T19:01:07.471511+00:00 | 1 | whatsapp | property_search | 12422.467 | None | 5 |
| 2026-11-03T19:01:20.461227+00:00 | 1 | website | payment_plan | 10990.454 | None | 5 |

---
*Disclaimer: These are local/development measurements, not production benchmarks. Actual production performance may vary.*
