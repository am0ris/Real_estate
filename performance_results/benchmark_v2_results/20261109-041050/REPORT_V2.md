# Benchmark v2 report (development measurements)

Generated: 2026-11-09T02:21:08.072242+00:00  |  run id: 20261109-041050  |  Python 3.12.6 on Windows

> Development, in-process measurements against real configured providers. Not production benchmarks.

## 0. Run configuration
- Tier: **standard**; concurrency mode: **cheap**; turns executed this session: 44 (provider-backed cost 42); daily turn budget used: 61/150.

## 1. Inventory used as ground truth
- Ground truth source: csv:scripts/test_properties_small.csv  |  listing sources: [['bayut.eg', 100]]
- Qdrant vs CSV: 100 in CSV, 100 in Qdrant, 0 CSV ids missing from Qdrant, 0 Qdrant ids not in CSV.
- Collection `properties`: 100 unique properties (0 real-named, 100 demo/test by file name); 0 non-property chunks.
- Ground truth includes demo listings: **True** (app SHOW_DEMO_LISTINGS=True).
- Price (EGP) min / median / max: 1000000.0 / 8495000.0 / 62173000.0
- Top cities: [['new cairo', 34], ['new capital city', 15], ['sheikh zayed', 14], ['6th of october', 13], ['mostakbal city', 4], ['ain sukhna', 4], ['new heliopolis', 3], ['north coast', 3]]
- Types: [['apartment', 70], ['villa', 11], ['townhouse', 7], ['chalet', 5], ['duplex', 3], ['twin house', 2], ['room', 1], ['penthouse', 1]]
- Bedrooms: [['3', 54], ['2', 26], ['4', 13], ['1', 4], ['None', 2], ['5', 1]]
- Transaction types: [['sale', 100]]
- Note: a non-demo *file name* shows the listing is not labelled as test data. It does not prove the data is a live feed; confirm the source with the business.

## 2. Retrieval quality (search turns only, single-user phase)
- Search turns: 35 (errors: 0). Queries where inventory has a match: 28; with no match: 7.
- Hit rate where matches exist: 100.0%  |  **False-empty rate**: 0.0% (matches exist but nothing returned)
- Mean precision of returned listings: 100.0%  |  Mean recall@5: 100.0%  |  MRR: 1.0
- Correct empties: 7  |  False positives (returned although nothing matches): 0  |  Wrong results: 0
- Returned ids not found in inventory: 0  |  Cited ids not grounded in returned/grounding ids: 0

| Language | Search turns | Hit | False empty | Correct empty | False positive | Wrong | Error |
|---|---|---|---|---|---|---|---|
| en | 21 | 17 | 0 | 4 | 0 | 0 | 0 |
| ar | 14 | 11 | 0 | 3 | 0 | 0 | 0 |

### Stale-filter check (second search in the same thread)
- en-stale-filters: outcome **hit** (gt=4, returned=4)
- ar-stale-filters: outcome **hit** (gt=4, returned=4)

## 3. Latency, single user
| Slice | n | Mean (s) | P50 (s) | P90 (s) | P95 (s) | Max (s) |
|---|---|---|---|---|---|---|
| All turns | 43 | 4.48 | 2.94 | 13.59 | 15.61 | 22.28 |
| kind=search | 35 | 5.42 | 3.37 | 13.75 | 17.09 | 22.28 |
| kind=followup | 4 | 0.44 | 0.39 | 0.75 | 0.79 | 0.83 |
| kind=compare | 1 | 0.99 | 0.99 | 0.99 | 0.99 | 0.99 |
| kind=book | 1 | 0.11 | 0.11 | 0.11 | 0.11 | 0.11 |
| kind=reset | 2 | 0.10 | 0.10 | 0.11 | 0.11 | 0.11 |
| lang=en | 26 | 3.17 | 2.88 | 4.37 | 10.93 | 13.75 |
| lang=ar | 17 | 6.49 | 3.37 | 17.52 | 20.51 | 22.28 |
| channel=website | 41 | 4.54 | 2.93 | 13.73 | 15.82 | 22.28 |
| channel=whatsapp | 2 | 3.21 | 3.21 | 3.34 | 3.36 | 3.37 |

Single-user success: 43/43 (95% CI 91.8% to 100.0%). Success = no exception and no `result.error`; it does not mean the answer was correct.

Follow-up turns: 4; ungrounded citations: 0

## 3b. Conversation checks (single user)
- 'Start over' turns: 2, outcomes {'reset_ok': 2}, median latency 0.10 s.
- Search after 'start over' (ar-reset): outcome **hit** (gt=4, returned=4)
- Search after 'start over' (en-reset): outcome **hit** (gt=4, returned=4)
- compare turns: 1, outcomes {'turn_ok': 1}
- book turns: 1, outcomes {'turn_ok': 1}
- 9-digit listing codes written in replies: not in inventory = **0** (possible hallucination); real but not shown = 0
- website reply length: median 156 chars, max 627; over 4096 chars: 0
- whatsapp reply length: median 737 chars, max 835; over 4096 chars: 0

## 4. Concurrency (all users start together; wall-clock throughput)
Mode: **cheap**. `cheap` sends 'start over' turns: it exercises the graph, per-conversation locking and Postgres checkpointing without provider calls, but it does not hold a lock during a model call, so it can under-state pool contention. Confirm any finding with a small `--conc-mode search` run (for example 5 users x 1 request).
Not run.

## 5. Understanding layer (rule-based, no provider calls)
- 42/42 checks passed.

## 6. What you can and cannot claim
- You can quote the numbers above with their conditions (development, in-process, n shown).
- You cannot call them production performance, and 'success' is not 'correct answer'.
- A false empty is a retrieval/filter problem; a correct empty means the inventory has no match (a content gap, not an AI failure).
- Ground truth uses name aliases and exact bedroom matching (see `--bedrooms-mode`); unusual field values in the inventory can cause mismatches. Inspect the failing rows before quoting.
