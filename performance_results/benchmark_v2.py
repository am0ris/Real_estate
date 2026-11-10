#!/usr/bin/env python3
"""
Real Estate AI Assistant - Benchmark v2
=======================================

Standalone script (put it in scripts/ next to benchmark.py). It does NOT modify
project code; it calls the real `conversation_service.run_turn()` in-process and
reads the Qdrant collection directly.

What is new compared with benchmark.py
--------------------------------------
1. GROUND TRUTH. The inventory is read straight from Qdrant, de-duplicated by
   property_id, and every search query is scored against what actually exists
   (non-demo listings only, unless demo listings are switched on). An empty
   result is therefore classified as a *correct empty* (nothing matches) or a
   *false empty* (matching listings exist but were not returned).
2. RETRIEVAL METRICS: precision of returned listings, recall@5, MRR, false-empty
   rate, false-positive rate, cited-id grounding check.
3. ONLY SEARCH TURNS count as retrieval. Greetings and follow-ups are reported
   separately (v1 mixed greetings into the retrieval sample).
4. BILINGUAL dataset (Arabic + English, `language` is passed per query), WhatsApp
   and website channels, multi-turn conversations in one thread, and a
   stale-filter check (search A, then a different search B in the same thread).
5. TRUE CONCURRENCY: all users start together (barrier), several requests per
   user, several repeats per level, wall-clock throughput, errors classified
   (pool_timeout / rate_limit / timeout / other), Wilson 95% intervals.
6. The stop rule is explicit and configurable (v1 stopped at exactly 20% errors).

Typical use (cheapest first; every step is optional)
---------------------------------------------------
  # 1. FREE: ground truth, inventory/Qdrant coverage, rule-based understanding checks (no provider calls)
  python scripts/benchmark_v2.py --dry-run --inventory-csv scripts/test_properties_small.csv
  # 2. Smoke retrieval + latency (~13 turns)
  python scripts/benchmark_v2.py --tier smoke --single-only --inventory-csv scripts/test_properties_small.csv
  # 3. Connection-pool test without provider calls ('start over' turns), then confirm with 5 real searches
  python scripts/benchmark_v2.py --conc-only --conc-mode cheap --levels 5,10,25 --inventory-csv ...
  python scripts/benchmark_v2.py --conc-only --conc-mode search --levels 5 --level-repeats 1 --reqs-per-user 1 --inventory-csv ...
  # 4. Broader coverage when quota allows
  python scripts/benchmark_v2.py --tier standard --single-only --inventory-csv ...
  # A halted run (rate limit or budget) continues with:  --resume <run id printed in the log>

Quota protection: tiers, a persistent daily turn budget (--daily-turn-budget), automatic halt after repeated
provider rate limits, resume, and a concurrency mode that does not call the providers.

All outputs go to scripts/benchmark_v2_results/ (CSV, JSON, REPORT_V2.md).
These are development measurements, not production benchmarks.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# --------------------------------------------------------------------------
# Aliases used ONLY for ground truth matching (edit to match your inventory)
# --------------------------------------------------------------------------
CITY_ALIASES: dict[str, list[str]] = {
    "new_cairo": ["new cairo", "5th settlement", "fifth settlement", "tagamoa", "التجمع", "القاهرة الجديدة"],
    "sheikh_zayed": ["sheikh zayed", "الشيخ زايد", "شيخ زايد"],
    "new_capital": ["new capital", "administrative capital", "العاصمة الادارية", "العاصمة الإدارية"],
    "october": ["6th of october", "6 october", "اكتوبر", "أكتوبر"],
    "ain_sukhna": ["ain sukhna", "العين السخنة"],
    "north_coast": ["north coast", "sahel", "الساحل الشمالي"],
    "maadi": ["maadi", "المعادي"],
    "madinaty": ["madinaty", "مدينتي"],
    "mostakbal": ["mostakbal", "المستقبل"],
}
TYPE_ALIASES: dict[str, list[str]] = {
    "apartment": ["apartment", "flat", "شقة", "شقق"],
    "villa": ["villa", "فيلا", "فلل"],
    "townhouse": ["townhouse", "town house", "تاون هاوس"],
    "twin_house": ["twin house", "twinhouse", "توين هاوس"],
    "duplex": ["duplex", "دوبلكس"],
    "chalet": ["chalet", "شاليه"],
    "studio": ["studio", "ستوديو"],
    "penthouse": ["penthouse", "بنتهاوس"],
}
LOCATION_FIELDS = ["location", "city", "district", "neighborhood", "compound",
                   "location_area", "location_district", "location_governorate"]
# payload / CSV metadata key variants
FIELD_ALIASES = {
    "property_type": ["property_type", "type"], "bedrooms": ["bedrooms", "beds"],
    "bathrooms": ["bathrooms", "baths"], "price": ["price", "price_egp"], "area": ["area", "area_sqm"],
}
K = 5  # recall@K

# Conversations included per tier (cumulative). Anything else is "full" only unless the JSON sets "tier".
SMOKE_IDS = {"en-nc-apt3", "en-nc-apt3-5m", "en-sz-villa", "en-sz-villa2", "ar-sz-villa", "ar-nc-apt3",
             "ar-sz-villa2", "ar-wa-sz-villa", "en-stale-filters", "ar-reset"}
STANDARD_IDS = SMOKE_IDS | {"en-nc-apt3-8m", "en-nc-apt2", "en-nc-offplan", "en-sz-villa3", "en-newcap-apt3",
                            "en-maadi-apt", "en-nc-rent", "en-wa-nc-apt3", "ar-nc-apt3-8m", "ar-sz-apt2",
                            "ar-newcap-apt3", "ar-maadi-apt", "ar-nc-rent", "ar-stale-filters", "en-followup",
                            "ar-followup", "en-reset", "en-compare", "en-book-by-id", "en-any-price"}
TIER_RANK = {"smoke": 0, "standard": 1, "full": 2}
# Quota proxy: how many "provider-backed" turns a turn kind costs. `reset` is answered with a fixed reply
# (no LLM / embedding) according to the project notes; the report checks this assumption from its latency.
TURN_COST = {"search": 1, "followup": 1, "compare": 1, "book": 1, "greeting": 1, "reset": 0}
CHEAP_TURN = {"kind": "reset", "text": "ابدأ من جديد"}
ID_RE = re.compile(r"\b\d{9}\b")


# --------------------------------------------------------------------------
# Built-in dataset (editable: use --write-dataset, edit the JSON, pass --dataset)
# --------------------------------------------------------------------------
def _search(text: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "search", "text": text, "spec": spec}


def builtin_dataset() -> list[dict[str, Any]]:
    """Tuned to test_properties_small.csv (100 Bayut sale listings). Expected match counts are noted."""
    D: list[dict[str, Any]] = []

    def conv(cid: str, lang: str, channel: str, *turns: dict[str, Any]) -> None:
        D.append({"id": cid, "lang": lang, "channel": channel, "turns": list(turns)})

    nc3 = {"city": "new_cairo", "type": "apartment", "bedrooms": 3}          # 14 in CSV
    szv = {"city": "sheikh_zayed", "type": "villa"}                          # 4
    # ---- English, website
    conv("en-nc-apt3", "en", "website", _search("3 bedroom apartment in New Cairo", nc3))
    conv("en-nc-apt3-8m", "en", "website", _search("3 bedroom apartment in New Cairo under 8 million EGP", {**nc3, "max_price": 8_000_000}))   # 8
    conv("en-nc-apt3-5m", "en", "website", _search("3 bedroom apartment in New Cairo under 5 million EGP", {**nc3, "max_price": 5_000_000}))   # 0 (negative)
    conv("en-nc-apt2", "en", "website", _search("2 bedroom apartment in New Cairo", {"city": "new_cairo", "type": "apartment", "bedrooms": 2}))  # 10
    conv("en-nc-townhouse", "en", "website", _search("townhouse in New Cairo", {"city": "new_cairo", "type": "townhouse"}))                  # 4
    conv("en-nc-offplan", "en", "website", _search("off-plan apartment in New Cairo", {"city": "new_cairo", "type": "apartment", "off_plan": True}))  # 9
    conv("en-sz-villa", "en", "website", _search("villa in Sheikh Zayed", szv))                                                           # 4
    conv("en-sz-villa3", "en", "website", _search("3 bedroom villa in Sheikh Zayed", {**szv, "bedrooms": 3}))                              # 3
    conv("en-sz-villa2", "en", "website", _search("2 bedroom villa in Sheikh Zayed", {**szv, "bedrooms": 2}))                              # 0 (negative)
    conv("en-sz-apt2", "en", "website", _search("2 bedroom apartment in Sheikh Zayed", {"city": "sheikh_zayed", "type": "apartment", "bedrooms": 2}))  # 3
    conv("en-newcap-apt3", "en", "website", _search("3 bedroom apartment in the New Capital", {"city": "new_capital", "type": "apartment", "bedrooms": 3}))  # 10
    conv("en-oct-apt", "en", "website", _search("apartment in 6th of October", {"city": "october", "type": "apartment"}))                  # 10
    conv("en-sukhna-chalet", "en", "website", _search("chalet in Ain Sukhna", {"city": "ain_sukhna", "type": "chalet"}))                    # 3
    conv("en-maadi-apt", "en", "website", _search("apartment in Maadi", {"city": "maadi", "type": "apartment"}))                          # 0 (negative)
    conv("en-nc-rent", "en", "website", _search("apartment for rent in New Cairo", {"city": "new_cairo", "type": "apartment", "transaction": "rent"}))  # 0 (negative)
    # ---- English, WhatsApp
    conv("en-wa-nc-apt3", "en", "whatsapp", _search("Hello, I need a 3 bedroom apartment in New Cairo", nc3))
    conv("en-wa-sz-villa", "en", "whatsapp", _search("Do you have villas in Sheikh Zayed?", szv))
    # ---- Arabic, website
    conv("ar-sz-villa", "ar", "website", _search("عايز فيلا في الشيخ زايد", szv))
    conv("ar-nc-apt3", "ar", "website", _search("عايز شقة 3 غرف في التجمع الخامس", nc3))
    conv("ar-nc-apt3-8m", "ar", "website", _search("عايز شقة 3 غرف في التجمع الخامس ميزانيتي لحد 8 مليون جنيه", {**nc3, "max_price": 8_000_000}))
    conv("ar-sz-apt2", "ar", "website", _search("شقة غرفتين في الشيخ زايد", {"city": "sheikh_zayed", "type": "apartment", "bedrooms": 2}))
    conv("ar-newcap-apt3", "ar", "website", _search("شقة 3 غرف في العاصمة الإدارية", {"city": "new_capital", "type": "apartment", "bedrooms": 3}))
    conv("ar-oct-apt", "ar", "website", _search("شقة في السادس من أكتوبر", {"city": "october", "type": "apartment"}))
    conv("ar-sz-villa2", "ar", "website", _search("فيلا غرفتين في الشيخ زايد", {**szv, "bedrooms": 2}))                                     # 0 (negative)
    conv("ar-maadi-apt", "ar", "website", _search("شقة في المعادي", {"city": "maadi", "type": "apartment"}))                              # 0 (negative)
    conv("ar-nc-rent", "ar", "website", _search("شقة للإيجار في التجمع الخامس", {"city": "new_cairo", "type": "apartment", "transaction": "rent"}))  # 0 (negative)
    # ---- Arabic, WhatsApp
    conv("ar-wa-sz-villa", "ar", "whatsapp", _search("مساء الخير، أبحث عن فيلا في الشيخ زايد", szv))
    conv("ar-wa-nc-apt2", "ar", "whatsapp", _search("شقة غرفتين في التجمع الخامس", {"city": "new_cairo", "type": "apartment", "bedrooms": 2}))
    # ---- Greetings (no retrieval expected)
    conv("en-greeting", "en", "website", {"kind": "greeting", "text": "Hello, how can you help me find a property?"})
    conv("ar-greeting", "ar", "website", {"kind": "greeting", "text": "السلام عليكم، ممكن تساعدني ألاقي عقار؟"})
    # ---- Multi-turn conversations in ONE thread
    conv("en-followup", "en", "website", _search("3 bedroom apartment in New Cairo", nc3),
         {"kind": "followup", "text": "What is the price of the first one?"},
         {"kind": "followup", "text": "Can I book a viewing for it?"})
    conv("ar-followup", "ar", "website", _search("عايز فيلا في الشيخ زايد", szv),
         {"kind": "followup", "text": "كام سعر أول وحدة؟"},
         {"kind": "followup", "text": "عايز أحجز معاينة"})
    # ---- Stale-filter check: a NEW search in the same thread must not inherit old filters
    conv("en-stale-filters", "en", "website",
         _search("3 bedroom apartment in New Cairo under 8 million EGP", {**nc3, "max_price": 8_000_000}),
         _search("Now show me villas in Sheikh Zayed", szv))
    conv("ar-stale-filters", "ar", "website",
         _search("عايز شقة 3 غرف في التجمع الخامس", nc3),
         _search("طيب وريني فيلل في الشيخ زايد", szv))
    # ---- "Start over" must clear carried filters (fixed reply, no provider call expected)
    conv("ar-reset", "ar", "website", _search("عايز شقة 3 غرف في التجمع الخامس", nc3),
         {"kind": "reset", "text": "ابدأ من جديد"},
         _search("فيلا في الشيخ زايد", szv))
    conv("en-reset", "en", "website", _search("3 bedroom apartment in New Cairo", nc3),
         {"kind": "reset", "text": "start over"},
         _search("villa in Sheikh Zayed", szv))
    # ---- Comparison and booking-by-code (code must not be read as a budget)
    conv("en-compare", "en", "website", _search("apartment in Sheikh Zayed", {"city": "sheikh_zayed", "type": "apartment"}),
         {"kind": "compare", "text": "Compare the first two"})
    conv("en-book-by-id", "en", "website", _search("villa in Sheikh Zayed", szv),
         {"kind": "book", "text": "I want to book a viewing for {first_id}"})
    # ---- "any price" must drop the old budget but keep city/type/bedrooms
    conv("en-any-price", "en", "website",
         _search("3 bedroom apartment in New Cairo under 8 million EGP", {**nc3, "max_price": 8_000_000}),
         _search("any price is fine", nc3))
    return D


def validate_dataset(ds: list[dict[str, Any]]) -> None:
    ids = set()
    for c in ds:
        for key in ("id", "lang", "channel", "turns"):
            if key not in c:
                raise SystemExit(f"dataset error: conversation missing '{key}': {c}")
        if c["id"] in ids:
            raise SystemExit(f"dataset error: duplicate id {c['id']}")
        ids.add(c["id"])
        if c["lang"] not in ("en", "ar"):
            raise SystemExit(f"dataset error: lang must be en/ar ({c['id']})")
        if c["channel"] not in ("website", "whatsapp"):
            raise SystemExit(f"dataset error: channel must be website/whatsapp ({c['id']})")
        for t in c["turns"]:
            if t.get("kind") not in ("search", "greeting", "followup", "reset", "compare", "book") or not t.get("text"):
                raise SystemExit(f"dataset error: bad turn in {c['id']}: {t}")
            spec = t.get("spec") or {}
            if t["kind"] == "search":
                if not spec:
                    raise SystemExit(f"dataset error: search turn without spec in {c['id']}")
                if spec.get("city") and spec["city"] not in CITY_ALIASES:
                    raise SystemExit(f"dataset error: unknown city alias '{spec['city']}' in {c['id']}")
                if spec.get("type") and spec["type"] not in TYPE_ALIASES:
                    raise SystemExit(f"dataset error: unknown type alias '{spec['type']}' in {c['id']}")


# --------------------------------------------------------------------------
# Small stats helpers
# --------------------------------------------------------------------------
def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (k - f) * (s[c] - s[f])


def lat_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": statistics.mean(values), "p50": percentile(values, 50),
            "p90": percentile(values, 90), "p95": percentile(values, 95), "max": max(values), "min": min(values)}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def sec(ms: float | None) -> str:
    return "n/a" if ms is None or (isinstance(ms, float) and math.isnan(ms)) else f"{ms / 1000:.2f}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_error(msg: str | None) -> str:
    if not msg:
        return ""
    m = msg.lower()
    squashed = re.sub(r"[^a-z0-9]", "", m)
    if "ratelimit" in squashed or "toomanyrequests" in squashed:
        return "rate_limit"
    if "pooltimeout" in m or "couldn't get a connection" in m or "couldn\u2019t get a connection" in m:
        return "pool_timeout"
    if "429" in m or "rate limit" in m or "rate_limit" in m or "too many requests" in m:
        return "rate_limit"
    if "timeout" in m or "timed out" in m:
        return "timeout"
    return "other"


# --------------------------------------------------------------------------
# Inventory (ground truth) from Qdrant
# --------------------------------------------------------------------------
_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ـ": ""})


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower().translate(_AR_MAP)).strip()


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return None
    return str(v).strip().lower() in ("true", "1", "yes")


def _finalize(props: dict[str, dict[str, Any]]) -> None:
    for rec in props.values():
        rec["city"] = rec.get("city") or rec.get("location_district") or rec.get("location") or ""
        rec["location_text"] = " | ".join(str(rec.get(f) or "") for f in LOCATION_FIELDS)
        if rec.get("bedrooms") is not None:
            rec["bedrooms"] = int(rec["bedrooms"])
        rec.setdefault("is_demo", False)


def load_csv_inventory(path: str) -> dict[str, dict[str, Any]]:
    """Ground truth from the CSV the model was ingested from (columns: id, text, metadata-as-JSON)."""
    props: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                meta = json.loads(row.get("metadata") or "{}")
            except json.JSONDecodeError:
                meta = {}
            pid = str(meta.get("property_id") or row.get("id") or "")
            if not pid:
                continue
            text = (row.get("text") or "").lower()
            rec: dict[str, Any] = {"property_id": pid, "is_demo": False, "source": meta.get("source", ""),
                                   "transaction_type": "rent" if " for rent" in text else "sale",
                                   "is_off_plan": _bool(meta.get("is_off_plan"))}
            for canon, keys in FIELD_ALIASES.items():
                for k in keys:
                    if meta.get(k) not in (None, ""):
                        rec[canon] = meta[k] if canon == "property_type" else _num(meta[k])
                        break
            for f in LOCATION_FIELDS:
                if meta.get(f):
                    rec[f] = str(meta[f])
            props[pid] = rec
    _finalize(props)
    return props


def _scroll_qdrant() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Scroll the whole collection; return ({property_id: record}, meta)."""
    from app.database.vector_db import create_client  # type: ignore
    from app.helpers.config import get_settings  # type: ignore

    try:
        from app.workflows.rag import is_demo_listing  # type: ignore
    except Exception:  # pragma: no cover
        def is_demo_listing(*_s: Any) -> bool:  # type: ignore
            return False
        print("WARNING: could not import is_demo_listing; demo listings will not be separated.")

    settings = get_settings()
    collection = settings.QDRANT_COLLECTION
    client = create_client()
    props: dict[str, dict[str, Any]] = {}
    chunks = non_property_chunks = 0
    try:
        offset = None
        while True:
            points, offset = client.scroll(collection_name=collection, limit=256, offset=offset,
                                           with_payload=True, with_vectors=False)
            for pt in points:
                chunks += 1
                p = pt.payload or {}
                meta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}

                def g(field: str) -> Any:
                    v = p.get(field)
                    return v if v not in (None, "") else meta.get(field)

                pid = g("property_id")
                if pid in (None, ""):
                    non_property_chunks += 1
                    continue
                pid = str(pid)
                rec = props.setdefault(pid, {"property_id": pid})
                for f in LOCATION_FIELDS + ["transaction_type", "availability", "currency", "source"]:
                    if not rec.get(f) and g(f):
                        rec[f] = str(g(f))
                for canon, keys in FIELD_ALIASES.items():
                    if rec.get(canon) in (None, ""):
                        for k in keys:
                            if g(k) not in (None, ""):
                                rec[canon] = str(g(k)) if canon == "property_type" else _num(g(k))
                                break
                if rec.get("is_off_plan") is None:
                    rec["is_off_plan"] = _bool(g("is_off_plan"))
                sources = (p.get("source_file"), g("file_name"), g("source_path"))
                rec["is_demo"] = rec.get("is_demo", False) or bool(is_demo_listing(*sources))
                rec["source_file"] = rec.get("source_file") or str(p.get("source_file") or g("file_name") or "")
            if offset is None:
                break
    finally:
        try:
            client.close()
        except Exception:
            pass
    _finalize(props)
    return props, {"collection": collection, "chunks_scanned": chunks, "non_property_chunks": non_property_chunks,
                   "app_show_demo_listings": bool(getattr(settings, "SHOW_DEMO_LISTINGS", False))}


def load_inventory(include_demo: bool | None, csv_path: str | None = None, use_qdrant: bool = True
                   ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Ground-truth inventory. With --inventory-csv the CSV is the truth and Qdrant is only compared against it."""
    q_props: dict[str, dict[str, Any]] | None = None
    q_meta: dict[str, Any] = {}
    if use_qdrant or not csv_path:
        try:
            q_props, q_meta = _scroll_qdrant()
        except Exception as exc:  # noqa: BLE001
            if not csv_path:
                raise
            q_meta = {"qdrant_error": f"{type(exc).__name__}: {exc}"}
            print(f"WARNING: could not read Qdrant ({exc}); continuing with the CSV only.")
    props = load_csv_inventory(csv_path) if csv_path else (q_props or {})
    if csv_path and q_props:
        for pid, rec in props.items():  # mirror the app's visibility: demo flag comes from the ingested record
            if pid in q_props:
                rec["is_demo"] = bool(q_props[pid].get("is_demo"))
    show_demo = bool(q_meta.get("app_show_demo_listings", False))
    use_demo = show_demo if include_demo is None else include_demo
    real = [r for r in props.values() if not r["is_demo"]]
    demo = [r for r in props.values() if r["is_demo"]]
    pool = list(props.values()) if use_demo else real

    def top(counter: Counter, n: int = 15) -> list[list[Any]]:
        return [[k, v] for k, v in counter.most_common(n)]

    prices = sorted(r["price"] for r in pool if r.get("price") is not None)
    profile: dict[str, Any] = {
        "ground_truth_source": f"csv:{csv_path}" if csv_path else "qdrant",
        "collection": q_meta.get("collection", "(not read)"), "chunks_scanned": q_meta.get("chunks_scanned"),
        "non_property_chunks": q_meta.get("non_property_chunks", 0),
        "unique_properties": len(props), "real_listings": len(real), "demo_listings": len(demo),
        "ground_truth_includes_demo": use_demo, "app_show_demo_listings": show_demo,
        "by_city": top(Counter(norm(r.get("city") or "?") for r in pool)),
        "by_district": top(Counter(norm(r.get("location_area") or r.get("district") or "?") for r in pool)),
        "by_type": top(Counter(norm(r.get("property_type") or "?") for r in pool)),
        "by_bedrooms": top(Counter(str(r.get("bedrooms")) for r in pool)),
        "by_transaction": top(Counter(norm(r.get("transaction_type") or "(none)") for r in pool)),
        "by_source": top(Counter(norm(r.get("source") or "?") for r in pool)),
        "price_min": prices[0] if prices else None,
        "price_median": statistics.median(prices) if prices else None,
        "price_max": prices[-1] if prices else None,
        "demo_source_files": sorted({r.get("source_file", "") for r in demo})[:10],
    }
    if q_meta.get("qdrant_error"):
        profile["qdrant_error"] = q_meta["qdrant_error"]
    if csv_path and q_props is not None:
        a_ids, b_ids = set(props), set(q_props)
        profile["coverage"] = {"csv_properties": len(a_ids), "qdrant_properties": len(b_ids),
                               "in_csv_not_in_qdrant": len(a_ids - b_ids), "in_qdrant_not_in_csv": len(b_ids - a_ids),
                               "missing_examples": sorted(a_ids - b_ids)[:10], "extra_examples": sorted(b_ids - a_ids)[:10]}
    return props, profile


def _alias(text: str, aliases: list[str]) -> bool:
    t = norm(text)
    return any(norm(a) in t for a in aliases)


def matches(spec: dict[str, Any], rec: dict[str, Any], include_demo: bool, bedrooms_mode: str = "exact") -> bool:
    if rec.get("is_demo") and not include_demo:
        return False
    if spec.get("city") and not _alias(rec.get("location_text", ""), CITY_ALIASES[spec["city"]]):
        return False
    if spec.get("type") and not _alias(rec.get("property_type", ""), TYPE_ALIASES[spec["type"]]):
        return False
    b = rec.get("bedrooms")
    if "bedrooms" in spec:
        if b is None or (b != spec["bedrooms"] if bedrooms_mode == "exact" else b < spec["bedrooms"]):
            return False
    if "min_bedrooms" in spec and (b is None or b < spec["min_bedrooms"]):
        return False
    price = rec.get("price")
    if "max_price" in spec and (price is None or price > spec["max_price"]):
        return False
    if "min_price" in spec and (price is None or price < spec["min_price"]):
        return False
    if "off_plan" in spec and rec.get("is_off_plan") is not bool(spec["off_plan"]):
        return False
    tx = norm(rec.get("transaction_type") or "")
    is_rent = "rent" in tx or "ايجار" in tx
    want = spec.get("transaction")
    if want == "rent" and not is_rent:
        return False
    if want == "sale" and is_rent:
        return False
    return True


# --------------------------------------------------------------------------
# Running turns
# --------------------------------------------------------------------------
def _pid(p: Any) -> str | None:
    if isinstance(p, dict):
        for key in ("property_id", "id", "listing_id"):
            if p.get(key) not in (None, ""):
                return str(p[key])
    elif p not in (None, ""):
        return str(p)
    return None


def run_turn(service: Any, thread_id: str, text: str, channel: str, lang: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    error = None
    res = None
    try:
        res = service.run_turn(thread_id=thread_id, text=text, channel=channel, customer_id=thread_id,
                               modality="text", language=lang)
        if getattr(res, "error", None):
            error = str(res.error)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    ms = (time.perf_counter() - t0) * 1000
    returned = [i for i in (_pid(p) for p in (getattr(res, "properties", None) or [])) if i]
    return {
        "latency_ms": ms, "error": error, "error_type": classify_error(error),
        "intent": getattr(res, "intent", "") or "", "agent": getattr(res, "agent", "") or "",
        "returned_ids": returned,
        "cited_ids": [str(i) for i in (getattr(res, "cited_property_ids", None) or [])],
        "grounding_ids": [str(i) for i in (getattr(res, "grounding_property_ids", None) or [])],
        "response": getattr(res, "response", "") or "",
    }


def score_search(spec: dict[str, Any], out: dict[str, Any], inv: dict[str, dict[str, Any]],
                 include_demo: bool, bedrooms_mode: str) -> dict[str, Any]:
    gt = {pid for pid, rec in inv.items() if matches(spec, rec, include_demo, bedrooms_mode)}
    returned = out["returned_ids"]
    matched = [i for i in returned if i in gt]
    unknown = [i for i in returned if i not in inv]
    if out["error"]:
        outcome = "error"
    elif gt and not returned:
        outcome = "false_empty"
    elif not gt and not returned:
        outcome = "correct_empty"
    elif not gt and returned:
        outcome = "false_positive"
    elif matched:
        outcome = "hit"
    else:
        outcome = "wrong_results"
    precision = (len(matched) / len(returned)) if returned else None
    recall = (len(set(returned[:K]) & gt) / min(len(gt), K)) if gt else None
    mrr = None
    if gt:
        mrr = 0.0
        for rank, i in enumerate(returned, 1):
            if i in gt:
                mrr = 1.0 / rank
                break
    allowed = set(returned) | set(out["grounding_ids"])
    cited_ungrounded = [i for i in out["cited_ids"] if i not in allowed]
    return {"n_gt": len(gt), "outcome": outcome, "precision": precision, "recall_at_5": recall, "mrr": mrr,
            "unknown_ids": len(unknown), "cited_ungrounded": len(cited_ungrounded)}


CSV_FIELDS = ["run_id", "phase", "level", "repeat", "user", "conv_id", "turn_idx", "lang", "channel", "kind",
              "text", "latency_ms", "error_type", "error", "intent", "agent", "n_returned", "n_gt", "outcome",
              "precision", "recall_at_5", "mrr", "unknown_ids", "cited_ungrounded", "text_ids_unknown",
              "text_ids_unreturned", "response_chars", "response_snippet"]


# --------------------------------------------------------------------------
# Quota protection: daily turn budget, halt flag, run metadata
# --------------------------------------------------------------------------
class Budget:
    """Persistent per-day counter of provider-backed turns (a proxy for free-tier usage)."""

    def __init__(self, path: Path, daily: int, force: bool) -> None:
        self.path, self.daily, self.force = path, daily, force
        self.lock = threading.Lock()
        self.date = datetime.now().strftime("%Y-%m-%d")
        data: dict[str, Any] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        self.used = int(data.get("used", 0)) if data.get("date") == self.date else 0

    def can(self, cost: int) -> bool:
        return self.force or cost == 0 or self.used + cost <= self.daily

    def add(self, cost: int) -> None:
        if not cost:
            return
        with self.lock:
            self.used += cost
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"date": self.date, "used": self.used}), encoding="utf-8")


BUDGET = Budget(Path("."), 10**9, True)
HALT = threading.Event()
RUN_META: dict[str, Any] = {"turns_executed": 0, "cost": 0, "halt_reason": ""}
_META_LOCK = threading.Lock()
_JSONL_LOCK = threading.Lock()


def timed_turn(service: Any, tid: str, text: str, channel: str, lang: str, kind: str) -> dict[str, Any] | None:
    cost = TURN_COST.get(kind, 1)
    if HALT.is_set():
        return None
    if not BUDGET.can(cost):
        RUN_META["halt_reason"] = RUN_META["halt_reason"] or f"daily turn budget reached ({BUDGET.used}/{BUDGET.daily})"
        HALT.set()
        return None
    out = run_turn(service, tid, text, channel, lang)
    BUDGET.add(cost)
    with _META_LOCK:
        RUN_META["turns_executed"] += 1
        RUN_META["cost"] += cost
    return out


def append_jsonl(path: Path | None, row: dict[str, Any]) -> None:
    if path is None:
        return
    with _JSONL_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def text_id_checks(out: dict[str, Any], inv: dict[str, dict[str, Any]], seen_ids: set[str]) -> tuple[int, int]:
    """9-digit codes written in the reply: unknown = not in inventory (hallucinated); unreturned = real but not shown."""
    codes = set(ID_RE.findall(out["response"]))
    shown = set(out["returned_ids"]) | set(out["grounding_ids"]) | seen_ids
    return (sum(1 for c in codes if c not in inv), sum(1 for c in codes if c in inv and c not in shown))


def make_row(run_id: str, phase: str, level: int, rep: int, user: int, conv_id: str, ti: int, lang: str,
             channel: str, kind: str, text: str, out: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id, "phase": phase, "level": level, "repeat": rep, "user": user, "conv_id": conv_id,
            "turn_idx": ti, "lang": lang, "channel": channel, "kind": kind, "text": text,
            "latency_ms": round(out["latency_ms"], 1), "error_type": out["error_type"], "error": out["error"] or "",
            "intent": out["intent"], "agent": out["agent"], "n_returned": len(out["returned_ids"]), "n_gt": "",
            "outcome": "", "precision": "", "recall_at_5": "", "mrr": "", "unknown_ids": "", "cited_ungrounded": "",
            "text_ids_unknown": "", "text_ids_unreturned": "", "response_chars": len(out["response"]),
            "response_snippet": out["response"][:300].replace("\n", " ")}


def fill_scores(row: dict[str, Any], turn: dict[str, Any], out: dict[str, Any], inv: dict[str, dict[str, Any]],
                include_demo: bool, bedrooms_mode: str, seen_ids: set[str]) -> None:
    kind = turn["kind"]
    if kind == "search":
        sc = score_search(turn["spec"], out, inv, include_demo, bedrooms_mode)
        row.update({k: ("" if v is None else (round(v, 4) if isinstance(v, float) else v)) for k, v in sc.items()})
    elif not out["error"]:
        if kind in ("followup", "compare", "book"):
            allowed = seen_ids | set(out["grounding_ids"]) | set(out["returned_ids"])
            row["cited_ungrounded"] = len([i for i in out["cited_ids"] if i not in allowed])
            row["outcome"] = "turn_ok"
        elif kind == "greeting":
            row["outcome"] = "greeting_no_retrieval" if not out["returned_ids"] else "greeting_retrieved"
        elif kind == "reset":
            row["outcome"] = "reset_ok" if not out["returned_ids"] else "reset_retrieved"
    if kind != "reset" and not out["error"]:
        unk, unret = text_id_checks(out, inv, seen_ids)
        row["text_ids_unknown"], row["text_ids_unreturned"] = unk, unret
    if out["error"]:
        row["outcome"] = "error"


def single_user_phase(service: Any, dataset: list[dict[str, Any]], inv: dict[str, dict[str, Any]], args: Any,
                      include_demo: bool, run_id: str, jsonl: Path | None,
                      done: set[tuple[str, int]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_turns = sum(len(c["turns"]) for c in dataset) * args.repeats
    n = 0
    consecutive_rl = 0
    for rep in range(args.repeats):
        for c in dataset:
            if (c["id"], rep) in done:
                n += len(c["turns"])
                continue
            tid = f"v2-{run_id}-single-{c['id']}-r{rep}{'-retry' if args.resume else ''}"
            seen_ids: set[str] = set()
            last_ids: list[str] = []
            for ti, turn in enumerate(c["turns"]):
                n += 1
                if HALT.is_set():
                    break
                text = turn["text"]
                if "{first_id}" in text or "{second_id}" in text:
                    need = 2 if "{second_id}" in text else 1
                    if len(last_ids) < need:
                        print(f"  [{n}/{total_turns}] {c['id']}#{ti} skipped (no listing returned earlier to reference)")
                        row = make_row(run_id, "single", 1, rep, 0, c["id"], ti, c["lang"], c["channel"], turn["kind"], text,
                                       {"latency_ms": 0, "error_type": "", "error": None, "intent": "", "agent": "",
                                        "returned_ids": [], "response": ""})
                        row["outcome"] = "skipped_no_listing"
                        rows.append(row)
                        append_jsonl(jsonl, row)
                        continue
                    text = text.replace("{first_id}", last_ids[0]).replace("{second_id}", last_ids[1] if len(last_ids) > 1 else "")
                out = timed_turn(service, tid, text, c["channel"], c["lang"], turn["kind"])
                if out is None:
                    break
                row = make_row(run_id, "single", 1, rep, 0, c["id"], ti, c["lang"], c["channel"], turn["kind"], text, out)
                fill_scores(row, turn, out, inv, include_demo, args.bedrooms_mode, seen_ids)
                if turn["kind"] == "search" and out["returned_ids"]:
                    seen_ids |= set(out["returned_ids"])
                    last_ids = list(out["returned_ids"])
                rows.append(row)
                append_jsonl(jsonl, row)
                print(f"  [{n}/{total_turns}] {c['id']}#{ti} {turn['kind']:<8} {row['latency_ms']/1000:6.1f}s "
                      f"returned={row['n_returned']} gt={row['n_gt']} -> {row['outcome'] or 'ok'}", flush=True)
                if out["error_type"] == "rate_limit":
                    consecutive_rl += 1
                    print(f"  provider rate limit hit ({consecutive_rl}/{args.max_rate_limit_errors}); cooling down {args.cooldown}s")
                    if consecutive_rl >= args.max_rate_limit_errors:
                        RUN_META["halt_reason"] = RUN_META["halt_reason"] or "stopped after repeated provider rate limits (quota protection)"
                        HALT.set()
                        break
                    time.sleep(args.cooldown)
                else:
                    consecutive_rl = 0
                if TURN_COST.get(turn["kind"], 1):
                    time.sleep(args.pace)
            if HALT.is_set():
                break
        if HALT.is_set():
            break
    return rows


def concurrent_phase(service: Any, search_pool: list[dict[str, Any]], inv: dict[str, dict[str, Any]], args: Any,
                     include_demo: bool, run_id: str, jsonl: Path | None,
                     done_levels: set[tuple[int, int]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    levels_out: list[dict[str, Any]] = []
    stopped_reason = ""
    mode = args.conc_mode
    for level in args.levels:
        if stopped_reason or HALT.is_set():
            levels_out.append({"level": level, "skipped": stopped_reason or RUN_META["halt_reason"] or "halted"})
            continue
        for rep in range(args.level_repeats):
            if (level, rep) in done_levels:
                continue
            if HALT.is_set():
                break
            print(f"\n=== concurrency {level} users x {args.reqs_per_user} requests [{mode}] (repeat {rep + 1}/{args.level_repeats}) ===")
            barrier = threading.Barrier(level)
            lock = threading.Lock()
            level_rows: list[dict[str, Any]] = []

            def pick(u: int, j: int) -> tuple[str, dict[str, Any], str, str]:
                use_search = mode == "search" or (mode == "mixed" and (u + j) % 3 == 0)
                if use_search and search_pool:
                    c = search_pool[(u * args.reqs_per_user + j + rep) % len(search_pool)]
                    return c["id"], c["turns"][0], c["lang"], c["channel"]
                return "cheap-reset", CHEAP_TURN, "ar", "website"

            def worker(u: int) -> None:
                try:
                    barrier.wait(timeout=60)
                except threading.BrokenBarrierError:
                    pass
                for j in range(args.reqs_per_user):
                    cid, turn, lang, channel = pick(u, j)
                    tid = f"v2-{run_id}-conc{level}-r{rep}-u{u}-q{j}"
                    out = timed_turn(service, tid, turn["text"], channel, lang, turn["kind"])
                    if out is None:
                        return
                    row = make_row(run_id, "concurrent", level, rep, u, cid, 0, lang, channel, turn["kind"], turn["text"], out)
                    fill_scores(row, turn, out, inv, include_demo, args.bedrooms_mode, set())
                    with lock:
                        level_rows.append(row)
                    append_jsonl(jsonl, row)
                    if args.conc_pace:
                        time.sleep(args.conc_pace)

            t0 = time.perf_counter()
            threads = [threading.Thread(target=worker, args=(u,)) for u in range(level)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            wall = time.perf_counter() - t0
            for r in level_rows:
                r["wall_s"] = round(wall, 4)
            rows.extend(level_rows)
            errs = [r for r in level_rows if r["error"]]
            etypes = Counter(r["error_type"] for r in errs)
            print(f"  done in {wall:.1f}s | requests={len(level_rows)} errors={len(errs)} {dict(etypes)}")
            levels_out.append({"level": level, "repeat": rep, "wall_s": wall, "requests": len(level_rows),
                               "errors": len(errs), "error_types": dict(etypes)})
            if etypes.get("rate_limit") and mode != "cheap":
                stopped_reason = "stopped: provider rate limit hit (quota protection)"
                break
        lvl_rows = [r for r in rows if r["level"] == level]
        err_rate = 100.0 * sum(1 for r in lvl_rows if r["error"]) / max(1, len(lvl_rows))
        if lvl_rows and err_rate >= args.stop_error_pct:
            stopped_reason = stopped_reason or f"stopped: error rate {err_rate:.0f}% at {level} users (>= {args.stop_error_pct}%)"
            print("  " + stopped_reason)
    return rows, levels_out


# --------------------------------------------------------------------------
# NLU checks (ZERO provider calls): the rule-based understanding layer vs the dataset specs
# --------------------------------------------------------------------------
NLU_EXTRA = [
    {"text": "مش أكتر من 6 مليون", "expect": {"max_price": 6_000_000}, "absent": ["min_price"]},
    {"text": "no more than 6 million", "expect": {"max_price": 6_000_000}, "absent": ["min_price"]},
    {"text": "3 غرف على الأقل", "expect": {"min_bedrooms": 3}, "absent": ["bedrooms"]},
    {"text": "at least 3 bedrooms", "expect": {"min_bedrooms": 3}, "absent": ["bedrooms"]},
    {"text": "I want to book a viewing for 501188214", "expect": {}, "absent": ["min_price", "max_price"]},
    {"text": "عايز شقة في التجمع الخامس", "expect": {}, "absent": ["bedrooms", "min_bedrooms"]},
]
NEW_SEARCH_POS = ["ابدأ من جديد", "ابدا من جديد", "start over", "new search"]
NEW_SEARCH_NEG = ["3 bedroom apartment in New Cairo", "عايز فيلا في الشيخ زايد"]


def run_nlu_checks(dataset: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        from app.agents.understanding import extract_requirements, wants_new_search  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"NLU check skipped (cannot import app.agents.understanding: {exc})")
        return None
    results: list[dict[str, Any]] = []
    for c in dataset:
        for ti, t in enumerate(c["turns"]):
            # later turns lean on requirements carried from earlier messages, so only first turns are checked in isolation
            if t["kind"] != "search" or "{" in t["text"] or ti > 0:
                continue
            spec = t["spec"]
            try:
                got = extract_requirements(t["text"]) or {}
            except Exception as exc:  # noqa: BLE001
                results.append({"id": c["id"], "text": t["text"], "problems": [f"exception {exc}"], "got": {}})
                continue
            p: list[str] = []
            if spec.get("type") and not _alias(str(got.get("property_type", "")), TYPE_ALIASES[spec["type"]]):
                p.append(f"type: expected {spec['type']}, got {got.get('property_type')!r}")
            if spec.get("city") and not _alias(str(got.get("district", "")) + " " + str(got.get("location", "")), CITY_ALIASES[spec["city"]]):
                p.append(f"place: expected {spec['city']}, got {got.get('district')!r}")
            if "bedrooms" in spec and got.get("bedrooms") != spec["bedrooms"]:
                p.append(f"bedrooms: expected {spec['bedrooms']}, got {got.get('bedrooms')}/{got.get('min_bedrooms')}")
            if "max_price" in spec and got.get("max_price") != spec["max_price"]:
                p.append(f"max_price: expected {spec['max_price']}, got {got.get('max_price')}")
            if spec.get("off_plan") and got.get("delivery") != "off-plan":
                p.append(f"off-plan: got delivery={got.get('delivery')!r}")
            if spec.get("transaction") == "rent" and got.get("transaction_type") != "rent":
                p.append(f"rent: got transaction_type={got.get('transaction_type')!r}")
            if "max_price" not in spec and "max_price" in got:
                p.append(f"unexpected max_price {got.get('max_price')}")
            results.append({"id": c["id"], "text": t["text"], "problems": p, "got": got})
    for case in NLU_EXTRA:
        got = extract_requirements(case["text"]) or {}
        p = [f"{k}: expected {v}, got {got.get(k)}" for k, v in case["expect"].items() if got.get(k) != v]
        p += [f"unexpected {k}={got[k]}" for k in case["absent"] if k in got]
        results.append({"id": "nlu-extra", "text": case["text"], "problems": p, "got": got})
    for text in NEW_SEARCH_POS:
        ok = bool(wants_new_search(text))
        results.append({"id": "new-search", "text": text, "problems": [] if ok else ["not recognised as a new-search request"], "got": {}})
    for text in NEW_SEARCH_NEG:
        ok = not wants_new_search(text)
        results.append({"id": "new-search-neg", "text": text, "problems": [] if ok else ["wrongly treated as 'start over'"], "got": {}})
    bad = [r for r in results if r["problems"]]
    return {"total": len(results), "passed": len(results) - len(bad), "failed": bad}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in ("", None) else None
    except (TypeError, ValueError):
        return None


def retrieval_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    s = [r for r in rows if r["kind"] == "search"]
    ok = [r for r in s if r["outcome"] != "error"]
    cnt = Counter(r["outcome"] for r in s)
    pos = [r for r in ok if (_f(r["n_gt"]) or 0) > 0]
    neg = [r for r in ok if (_f(r["n_gt"]) or 0) == 0]
    prec = [_f(r["precision"]) for r in ok if _f(r["precision"]) is not None]
    rec = [_f(r["recall_at_5"]) for r in pos if _f(r["recall_at_5"]) is not None]
    mrr = [_f(r["mrr"]) for r in pos if _f(r["mrr"]) is not None]
    return {
        "search_turns": len(s), "errors": cnt.get("error", 0), "outcomes": dict(cnt),
        "queries_with_matching_inventory": len(pos), "queries_with_no_matching_inventory": len(neg),
        "false_empty": cnt.get("false_empty", 0), "false_positive": cnt.get("false_positive", 0),
        "wrong_results": cnt.get("wrong_results", 0), "hit": cnt.get("hit", 0), "correct_empty": cnt.get("correct_empty", 0),
        "false_empty_rate_on_positive": (cnt.get("false_empty", 0) / len(pos)) if pos else None,
        "hit_rate_on_positive": (cnt.get("hit", 0) / len(pos)) if pos else None,
        "mean_precision": statistics.mean(prec) if prec else None,
        "mean_recall_at_5": statistics.mean(rec) if rec else None,
        "mrr": statistics.mean(mrr) if mrr else None,
        "unknown_ids": sum(int(_f(r["unknown_ids"]) or 0) for r in s),
        "cited_ungrounded": sum(int(_f(r["cited_ungrounded"]) or 0) for r in rows),
    }


def write_outputs(out_dir: Path, rows: list[dict[str, Any]], levels_out: list[dict[str, Any]], profile: dict[str, Any],
                  args: Any, run_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "raw_requests.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    (out_dir / "inventory_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    single = [r for r in rows if r["phase"] == "single"]
    L: list[str] = []
    A = L.append
    A("# Benchmark v2 report (development measurements)")
    A("")
    A(f"Generated: {now_iso()}  |  run id: {run_id}  |  Python {platform.python_version()} on {platform.system()}")
    A("")
    A("> Development, in-process measurements against real configured providers. Not production benchmarks.")
    A("")
    A("## 0. Run configuration")
    A(f"- Tier: **{RUN_META.get('tier')}**; concurrency mode: **{RUN_META.get('conc_mode')}**; turns executed this session: "
      f"{RUN_META['turns_executed']} (provider-backed cost {RUN_META['cost']}); daily turn budget used: {BUDGET.used}/{BUDGET.daily}.")
    if RUN_META.get("halt_reason"):
        A(f"- **Run halted early:** {RUN_META['halt_reason']}. Results below are partial; use `--resume {run_id}` later.")
    A("")
    A("## 1. Inventory used as ground truth")
    A(f"- Ground truth source: {profile['ground_truth_source']}  |  listing sources: {profile.get('by_source')}")
    if profile.get("coverage"):
        cv = profile["coverage"]
        A(f"- Qdrant vs CSV: {cv['csv_properties']} in CSV, {cv['qdrant_properties']} in Qdrant, {cv['in_csv_not_in_qdrant']} CSV ids missing from Qdrant, {cv['in_qdrant_not_in_csv']} Qdrant ids not in CSV.")
    A(f"- Collection `{profile['collection']}`: {profile['unique_properties']} unique properties "
      f"({profile['real_listings']} real-named, {profile['demo_listings']} demo/test by file name); "
      f"{profile['non_property_chunks']} non-property chunks.")
    A(f"- Ground truth includes demo listings: **{profile['ground_truth_includes_demo']}** (app SHOW_DEMO_LISTINGS={profile['app_show_demo_listings']}).")
    A(f"- Price (EGP) min / median / max: {profile['price_min']} / {profile['price_median']} / {profile['price_max']}")
    A(f"- Top cities: {profile['by_city'][:8]}")
    A(f"- Types: {profile['by_type']}")
    A(f"- Bedrooms: {profile['by_bedrooms']}")
    A(f"- Transaction types: {profile['by_transaction']}")
    A("- Note: a non-demo *file name* shows the listing is not labelled as test data. It does not prove the data is a live feed; confirm the source with the business.")
    A("")
    # ---- retrieval
    A("## 2. Retrieval quality (search turns only, single-user phase)")
    rs = retrieval_summary(single)
    A(f"- Search turns: {rs['search_turns']} (errors: {rs['errors']}). Queries where inventory has a match: "
      f"{rs['queries_with_matching_inventory']}; with no match: {rs['queries_with_no_matching_inventory']}.")
    A(f"- Hit rate where matches exist: {pct(rs['hit_rate_on_positive'])}  |  **False-empty rate**: {pct(rs['false_empty_rate_on_positive'])} "
      f"(matches exist but nothing returned)")
    A(f"- Mean precision of returned listings: {pct(rs['mean_precision'])}  |  Mean recall@{K}: {pct(rs['mean_recall_at_5'])}  |  MRR: "
      f"{'n/a' if rs['mrr'] is None else round(rs['mrr'], 3)}")
    A(f"- Correct empties: {rs['correct_empty']}  |  False positives (returned although nothing matches): {rs['false_positive']}  |  "
      f"Wrong results: {rs['wrong_results']}")
    A(f"- Returned ids not found in inventory: {rs['unknown_ids']}  |  Cited ids not grounded in returned/grounding ids: {rs['cited_ungrounded']}")
    A("")
    A("| Language | Search turns | Hit | False empty | Correct empty | False positive | Wrong | Error |")
    A("|---|---|---|---|---|---|---|---|")
    for lang in ("en", "ar"):
        sub = [r for r in single if r["kind"] == "search" and r["lang"] == lang]
        c = Counter(r["outcome"] for r in sub)
        A(f"| {lang} | {len(sub)} | {c['hit']} | {c['false_empty']} | {c['correct_empty']} | {c['false_positive']} | {c['wrong_results']} | {c['error']} |")
    A("")
    bad = [r for r in single if r["kind"] == "search" and r["outcome"] in ("false_empty", "wrong_results", "false_positive")]
    if bad:
        A("### Search turns that failed the ground-truth check")
        A("| conv | lang | query | gt | returned | outcome |")
        A("|---|---|---|---|---|---|")
        for r in bad:
            A(f"| {r['conv_id']} | {r['lang']} | {r['text']} | {r['n_gt']} | {r['n_returned']} | {r['outcome']} |")
        A("")
    stale = [r for r in single if r["conv_id"].endswith("stale-filters") and r["turn_idx"] == 1]
    if stale:
        A("### Stale-filter check (second search in the same thread)")
        for r in stale:
            A(f"- {r['conv_id']}: outcome **{r['outcome']}** (gt={r['n_gt']}, returned={r['n_returned']})")
        A("")
    # ---- latency
    A("## 3. Latency, single user")
    A("| Slice | n | Mean (s) | P50 (s) | P90 (s) | P95 (s) | Max (s) |")
    A("|---|---|---|---|---|---|---|")

    def lat_row(label: str, sub: list[dict[str, Any]]) -> None:
        v = [float(r["latency_ms"]) for r in sub if not r["error"]]
        s = lat_stats(v)
        if s["n"] == 0:
            A(f"| {label} | 0 | | | | | |")
        else:
            A(f"| {label} | {s['n']} | {sec(s['mean'])} | {sec(s['p50'])} | {sec(s['p90'])} | {sec(s['p95'])} | {sec(s['max'])} |")

    lat_row("All turns", single)
    for kind in ("search", "followup", "compare", "book", "greeting", "reset"):
        sub = [r for r in single if r["kind"] == kind]
        if sub:
            lat_row(f"kind={kind}", sub)
    for lang in ("en", "ar"):
        lat_row(f"lang={lang}", [r for r in single if r["lang"] == lang])
    for ch in ("website", "whatsapp"):
        lat_row(f"channel={ch}", [r for r in single if r["channel"] == ch])
    k_ok = sum(1 for r in single if not r["error"])
    lo, hi = wilson(k_ok, len(single))
    A("")
    A(f"Single-user success: {k_ok}/{len(single)} (95% CI {pct(lo)} to {pct(hi)}). Success = no exception and no `result.error`; it does not mean the answer was correct.")
    A("")
    # ---- followups
    fu = [r for r in single if r["kind"] == "followup"]
    if fu:
        ung = sum(int(_f(r["cited_ungrounded"]) or 0) for r in fu)
        A(f"Follow-up turns: {len(fu)}; ungrounded citations: {ung}")
        A("")
    # ---- conversation checks
    A("## 3b. Conversation checks (single user)")
    resets = [r for r in single if r["kind"] == "reset"]
    if resets:
        v = [float(r["latency_ms"]) for r in resets if not r["error"]]
        p50 = percentile(v, 50) if v else float("nan")
        slow = bool(v) and p50 > 3000
        A(f"- 'Start over' turns: {len(resets)}, outcomes {dict(Counter(r['outcome'] for r in resets))}, median latency {sec(p50)} s."
          + (" That is slow for a fixed reply: reset turns look provider-backed, so `--conc-mode cheap` is NOT quota-free." if slow else ""))
    for r in [r for r in single if r["conv_id"].endswith("-reset") and r["turn_idx"] == 2]:
        A(f"- Search after 'start over' ({r['conv_id']}): outcome **{r['outcome']}** (gt={r['n_gt']}, returned={r['n_returned']})")
    for kind in ("compare", "book"):
        sub = [r for r in single if r["kind"] == kind]
        if sub:
            A(f"- {kind} turns: {len(sub)}, outcomes {dict(Counter(r['outcome'] for r in sub))}")
    tu = sum(int(_f(r["text_ids_unknown"]) or 0) for r in single)
    tr = sum(int(_f(r["text_ids_unreturned"]) or 0) for r in single)
    A(f"- 9-digit listing codes written in replies: not in inventory = **{tu}** (possible hallucination); real but not shown = {tr}")
    for ch in ("website", "whatsapp"):
        ln = [int(r["response_chars"]) for r in single if r["channel"] == ch and str(r["response_chars"]) != ""]
        if ln:
            A(f"- {ch} reply length: median {int(statistics.median(ln))} chars, max {max(ln)}; over 4096 chars: {sum(1 for x in ln if x > 4096)}")
    A("")
    # ---- concurrency
    conc = [r for r in rows if r["phase"] == "concurrent"]
    A("## 4. Concurrency (all users start together; wall-clock throughput)")
    A(f"Mode: **{RUN_META.get('conc_mode')}**. `cheap` sends 'start over' turns: it exercises the graph, per-conversation locking and Postgres "
      f"checkpointing without provider calls, but it does not hold a lock during a model call, so it can under-state pool contention. "
      f"Confirm any finding with a small `--conc-mode search` run (for example 5 users x 1 request).")
    if not conc:
        A("Not run.")
    else:
        A("| Users | Runs | Requests | Success | 95% CI | P50 (s) | P95 (s) | Max (s) | Throughput (req/s, wall) | Error types |")
        A("|---|---|---|---|---|---|---|---|---|---|")
        for level in sorted({r["level"] for r in conc}):
            sub = [r for r in conc if r["level"] == level]
            ok = [r for r in sub if not r["error"]]
            lo, hi = wilson(len(ok), len(sub))
            walls = {(r["repeat"]): r["wall_s"] for r in sub}
            thr = len(sub) / sum(walls.values()) if walls and sum(walls.values()) > 0 else float("nan")
            v = [float(r["latency_ms"]) for r in ok]
            et = dict(Counter(r["error_type"] for r in sub if r["error"]))
            A(f"| {level} | {len(walls)} | {len(sub)} | {len(ok)}/{len(sub)} ({pct(len(ok)/len(sub))}) | {pct(lo)} to {pct(hi)} | "
              f"{sec(percentile(v, 50))} | {sec(percentile(v, 95))} | {sec(max(v) if v else None)} | {thr:.3f} | {et or '-'} |")
        skipped = [lv for lv in levels_out if lv.get("skipped")]
        for lv in skipped:
            A(f"\n- Level {lv['level']} skipped ({lv['skipped']}).")
        A("")
        A("Error types: `pool_timeout` = could not get a DB connection (application capacity); `rate_limit` = provider quota (429), "
          "not an application bug; `timeout` = request exceeded a client/provider timeout.")
    A("")
    nlu = RUN_META.get("nlu")
    A("## 5. Understanding layer (rule-based, no provider calls)")
    if nlu:
        A(f"- {nlu['passed']}/{nlu['total']} checks passed.")
        for r in nlu["failed"][:25]:
            A(f"  - [{r['id']}] {r['text']} -> {'; '.join(r['problems'])}")
    else:
        A("- Not run (module could not be imported).")
    A("")
    A("## 6. What you can and cannot claim")
    A("- You can quote the numbers above with their conditions (development, in-process, n shown).")
    A("- You cannot call them production performance, and 'success' is not 'correct answer'.")
    A("- A false empty is a retrieval/filter problem; a correct empty means the inventory has no match (a content gap, not an AI failure).")
    A("- Ground truth uses name aliases and exact bedroom matching (see `--bedrooms-mode`); unusual field values in the inventory can cause mismatches. Inspect the failing rows before quoting.")
    (out_dir / "REPORT_V2.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({"retrieval": retrieval_summary(single), "levels": levels_out,
                                                       "run_meta": {k: v for k, v in RUN_META.items() if k != "nlu"},
                                                       "args": {k: str(v) for k, v in vars(args).items()}},
                                                      ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------
def select_dataset(ds: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    rank = TIER_RANK[tier]
    out = []
    for c in ds:
        t = c.get("tier") or ("smoke" if c["id"] in SMOKE_IDS else "standard" if c["id"] in STANDARD_IDS else "full")
        if TIER_RANK[t] <= rank:
            out.append(c)
    return out


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def main() -> None:
    global BUDGET
    ap = argparse.ArgumentParser(description="Real Estate AI Assistant benchmark v2 (quota-aware)")
    ap.add_argument("--dataset", help="JSON dataset (see --write-dataset)")
    ap.add_argument("--write-dataset", metavar="PATH", help="write the built-in dataset to PATH and exit")
    ap.add_argument("--dry-run", action="store_true", help="inventory + ground truth + NLU checks; NO provider calls")
    ap.add_argument("--nlu-only", action="store_true", help="only the rule-based understanding checks; NO provider calls")
    ap.add_argument("--tier", choices=["smoke", "standard", "full"], default="smoke",
                    help="how many conversations to run (smoke ~13 turns, standard ~40, full = all)")
    ap.add_argument("--single-only", action="store_true", help="skip the concurrency phase")
    ap.add_argument("--conc-only", action="store_true", help="skip the single-user phase (run only the concurrency phase)")
    ap.add_argument("--repeats", type=int, default=1, help="repeat the single-user dataset N times")
    ap.add_argument("--pace", type=float, default=2.0, help="seconds between provider-backed sequential turns")
    ap.add_argument("--levels", default="5,10,25", help="comma-separated concurrent user counts")
    ap.add_argument("--reqs-per-user", type=int, default=2)
    ap.add_argument("--level-repeats", type=int, default=2)
    ap.add_argument("--conc-mode", choices=["cheap", "search", "mixed"], default="cheap",
                    help="cheap = 'start over' turns (no provider calls expected); search = real searches; mixed = 1 in 3 searches")
    ap.add_argument("--conc-pace", type=float, default=0.0)
    ap.add_argument("--stop-error-pct", type=float, default=50.0, help="stop ascending when a level reaches this error rate")
    ap.add_argument("--warmup", choices=["search", "none"], default="search", help="one uncounted search before measuring (costs 1 turn)")
    ap.add_argument("--daily-turn-budget", type=int, default=150,
                    help="max provider-backed turns per calendar day across runs (set from your Groq/Gemini dashboards)")
    ap.add_argument("--force-budget", action="store_true", help="ignore the daily turn budget")
    ap.add_argument("--max-rate-limit-errors", type=int, default=3, help="halt after this many consecutive provider rate limits")
    ap.add_argument("--cooldown", type=float, default=30.0, help="seconds to wait after a rate limit")
    ap.add_argument("--run-id", help="fixed run id (needed for --resume)")
    ap.add_argument("--resume", metavar="RUN_ID", help="continue a halted run: finished conversations/levels are skipped")
    ap.add_argument("--bedrooms-mode", choices=["exact", "min"], default="exact")
    ap.add_argument("--inventory-csv", help="ground truth from this CSV (id,text,metadata JSON); Qdrant is then only cross-checked")
    ap.add_argument("--no-qdrant", action="store_true", help="do not read Qdrant (CSV ground truth only)")
    ap.add_argument("--include-demo", action="store_true", help="count demo listings in ground truth")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "benchmark_v2_results"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation before calling providers")
    args = ap.parse_args()
    args.levels = [int(x) for x in str(args.levels).split(",") if x.strip()]
    random.seed(args.seed)

    if args.write_dataset:
        Path(args.write_dataset).write_text(json.dumps(builtin_dataset(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dataset written to {args.write_dataset}")
        return

    full = json.loads(Path(args.dataset).read_text(encoding="utf-8")) if args.dataset else builtin_dataset()
    validate_dataset(full)
    out_dir = Path(args.out_dir)
    free_only = args.dry_run or args.nlu_only
    dataset = full if free_only else select_dataset(full, args.tier)

    print("Loading ground-truth inventory...")
    inv, profile = load_inventory(True if args.include_demo else None, args.inventory_csv, not args.no_qdrant)
    include_demo = profile["ground_truth_includes_demo"]
    print(json.dumps({k: profile[k] for k in ("collection", "unique_properties", "real_listings", "demo_listings",
                                              "non_property_chunks", "by_city", "by_type", "by_bedrooms",
                                              "by_transaction", "by_source", "price_min", "price_median", "price_max",
                                              "ground_truth_source")}, ensure_ascii=False))
    if profile.get("coverage"):
        print("\nQdrant vs CSV coverage:", json.dumps(profile["coverage"], ensure_ascii=False))
    print("\nGround truth per search query:")
    zero = 0
    for c in dataset:
        for t in c["turns"]:
            if t["kind"] != "search":
                continue
            n = sum(1 for r in inv.values() if matches(t["spec"], r, include_demo, args.bedrooms_mode))
            zero += n == 0
            flag = "  <-- NO MATCHING INVENTORY (expect an empty result)" if n == 0 else ""
            print(f"  {c['id']:<22} {c['lang']} matches={n:<4} {t['text']}{flag}")

    # free checks (no provider calls)
    nlu = run_nlu_checks(dataset)
    if nlu:
        RUN_META["nlu"] = nlu
        print(f"\nNLU checks (rule-based, free): {nlu['passed']}/{nlu['total']} passed")
        for r in nlu["failed"][:25]:
            print(f"  FAIL [{r['id']}] {r['text']} -> {'; '.join(r['problems'])}")
    if free_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        if nlu:
            lines = [f"# NLU check ({now_iso()})", f"{nlu['passed']}/{nlu['total']} passed", ""]
            lines += [f"- [{r['id']}] {r['text']} -> {'; '.join(r['problems'])}" for r in nlu["failed"]]
            (out_dir / "NLU_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nFree checks complete. {zero} search queries have no matching inventory (they test correct-empty behaviour).")
        return

    # ---- plan and quota guard
    BUDGET = Budget(out_dir.parent / ".benchmark_budget.json", args.daily_turn_budget, args.force_budget)
    search_pool = [c for c in dataset if len(c["turns"]) == 1 and c["turns"][0]["kind"] == "search"]
    n_single = 0 if args.conc_only else sum(TURN_COST.get(t["kind"], 1) for c in dataset for t in c["turns"]) * args.repeats
    n_turns = 0 if args.conc_only else sum(len(c["turns"]) for c in dataset) * args.repeats
    per_level = {"cheap": 0.0, "mixed": 1 / 3, "search": 1.0}[args.conc_mode]
    n_conc = 0 if args.single_only else int(sum(args.levels) * args.reqs_per_user * args.level_repeats * per_level)
    n_warm = 1 if (args.warmup == "search" and not (args.conc_only and args.conc_mode == "cheap")) else 0
    planned = n_single + n_conc + n_warm
    print(f"\nTier '{args.tier}': {len(dataset)} conversations, {n_turns} turns. Concurrency mode '{args.conc_mode}'.")
    print(f"Planned provider-backed turns: ~{planned} (single {n_single} + concurrent {n_conc} + warm-up {n_warm}); "
          f"daily budget used so far {BUDGET.used}/{BUDGET.daily}.")
    print("Each search turn typically uses 1 Gemini embedding + 1-2 Groq calls; follow-ups/greetings 1-2 Groq calls; 'start over' none.")
    if not args.force_budget and BUDGET.used + planned > BUDGET.daily:
        print(f"\nABORTED: this plan would exceed the daily turn budget ({BUDGET.used}+{planned} > {BUDGET.daily}).\n"
              f"Use a smaller --tier, --levels or --level-repeats, wait until tomorrow, or raise --daily-turn-budget / --force-budget "
              f"after checking your provider quotas.")
        sys.exit(2)
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    from app.agents.service import conversation_service  # type: ignore

    run_id = args.resume or args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = out_dir / run_id  # one folder per run so a later run never overwrites an earlier report
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "raw_requests.jsonl"
    rows: list[dict[str, Any]] = []
    done_convs: set[tuple[str, int]] = set()
    done_levels: set[tuple[int, int]] = set()
    if args.resume:
        raw = [r for r in load_jsonl(jsonl) if r.get("run_id") == run_id]
        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in raw:  # append-only file: the newest row for a single-user turn wins
            if r["phase"] == "single":
                latest[(r["conv_id"], int(r["repeat"]), int(r["turn_idx"]))] = r
        single_rows = list(latest.values())
        conc_rows = [r for r in raw if r["phase"] == "concurrent"]
        by_conv: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for r in single_rows:
            by_conv[(r["conv_id"], int(r["repeat"]))].append(r)
        for c in dataset:
            for rep in range(args.repeats):
                got = by_conv.get((c["id"], rep), [])
                if len(got) >= len(c["turns"]) and not any(r.get("error") for r in got):
                    done_convs.add((c["id"], rep))
        by_level: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for r in conc_rows:
            by_level[(int(r["level"]), int(r["repeat"]))].append(r)
        for key, lst in by_level.items():
            if len(lst) >= key[0] * args.reqs_per_user:
                done_levels.add(key)
        rows = [r for r in single_rows if (r["conv_id"], int(r["repeat"])) in done_convs]
        rows += [r for r in conc_rows if (int(r["level"]), int(r["repeat"])) in done_levels]
        print(f"Resuming run {run_id}: keeping {len(rows)} finished rows; {len(done_convs)} conversations and "
              f"{len(done_levels)} level runs are complete, the rest will be re-run (conversations whose turns hit errors are retried).")
    RUN_META.update({"tier": args.tier, "conc_mode": args.conc_mode, "run_id": run_id})

    if n_warm and not args.resume:
        print("\nWarm-up (1 search, not recorded)...")
        timed_turn(conversation_service, f"v2-{run_id}-warmup", "3 bedroom apartment in New Cairo", "website", "en", "search")
        time.sleep(args.pace)

    print("\n=== Single-user phase ===")
    if not args.conc_only:
        rows.extend(single_user_phase(conversation_service, dataset, inv, args, include_demo, run_id, jsonl, done_convs))
    levels_out: list[dict[str, Any]] = []
    if not args.single_only and not HALT.is_set():
        crows, levels_out = concurrent_phase(conversation_service, search_pool, inv, args, include_demo, run_id, jsonl, done_levels)
        rows.extend(crows)
    if RUN_META.get("halt_reason"):
        print(f"\nRUN HALTED: {RUN_META['halt_reason']}. Re-run later with --resume {run_id}.")
    write_outputs(out_dir, rows, levels_out, profile, args, run_id)
    print(f"\nDone. Results in {out_dir}\n  REPORT_V2.md, raw_requests.csv, raw_requests.jsonl, inventory_profile.json, summary.json")
    print(f"Provider-backed turns used today: {BUDGET.used}/{BUDGET.daily}")


if __name__ == "__main__":
    main()
