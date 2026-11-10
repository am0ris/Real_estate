# Benchmark v2 report (development measurements)

Generated: 2026-11-10T15:10:18.060001+00:00  |  run id: 20261110-170712  |  Python 3.12.6 on Windows

> Development, in-process measurements against real configured providers. Not production benchmarks.

## 0. Run configuration
- Tier: **smoke**; concurrency mode: **search**; turns executed this session: 21 (provider-backed cost 21); daily turn budget used: 21/150.

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
- Search turns: 0 (errors: 0). Queries where inventory has a match: 0; with no match: 0.
- Hit rate where matches exist: n/a  |  **False-empty rate**: n/a (matches exist but nothing returned)
- Mean precision of returned listings: n/a  |  Mean recall@5: n/a  |  MRR: n/a
- Correct empties: 0  |  False positives (returned although nothing matches): 0  |  Wrong results: 0
- Returned ids not found in inventory: 0  |  Cited ids not grounded in returned/grounding ids: 0

| Language | Search turns | Hit | False empty | Correct empty | False positive | Wrong | Error |
|---|---|---|---|---|---|---|---|
| en | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ar | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## 3. Latency, single user
| Slice | n | Mean (s) | P50 (s) | P90 (s) | P95 (s) | Max (s) |
|---|---|---|---|---|---|---|
| All turns | 0 | | | | | |
| lang=en | 0 | | | | | |
| lang=ar | 0 | | | | | |
| channel=website | 0 | | | | | |
| channel=whatsapp | 0 | | | | | |

Single-user success: 0/0 (95% CI n/a to n/a). Success = no exception and no `result.error`; it does not mean the answer was correct.

## 3b. Conversation checks (single user)
- 9-digit listing codes written in replies: not in inventory = **0** (possible hallucination); real but not shown = 0

## 4. Concurrency (all users start together; wall-clock throughput)
Mode: **search**. `cheap` sends 'start over' turns: it exercises the graph, per-conversation locking and Postgres checkpointing without provider calls, but it does not hold a lock during a model call, so it can under-state pool contention. Confirm any finding with a small `--conc-mode search` run (for example 5 users x 1 request).
| Users | Runs | Requests | Success | 95% CI | P50 (s) | P95 (s) | Max (s) | Throughput (req/s, wall) | Error types |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 2 | 20 | 18/20 (90.0%) | 69.9% to 97.2% | 31.00 | 36.70 | 41.99 | 0.136 | {'rate_limit': 2} |

Error types: `pool_timeout` = could not get a DB connection (application capacity); `rate_limit` = provider quota (429), not an application bug; `timeout` = request exceeded a client/provider timeout.

## 5. Understanding layer (rule-based, no provider calls)
- 22/22 checks passed.

## 6. What you can and cannot claim
- You can quote the numbers above with their conditions (development, in-process, n shown).
- You cannot call them production performance, and 'success' is not 'correct answer'.
- A false empty is a retrieval/filter problem; a correct empty means the inventory has no match (a content gap, not an AI failure).
- Ground truth uses name aliases and exact bedroom matching (see `--bedrooms-mode`); unusual field values in the inventory can cause mismatches. Inspect the failing rows before quoting.
