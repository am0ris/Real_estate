"""Check and clean a listings CSV before ingesting it.

Reads a CSV with the columns ``id, text, metadata`` (metadata is JSON, as in
``data/raw/properties``), reports what looks wrong and writes a cleaned copy:

* rows that cannot be real listings are dropped: no/implausible price, an area under 20 m² (a
  "10 m² apartment for EGP 13.2 million" was shown to a customer), or a price per m² far outside the
  market range;
* the district is rewritten to its canonical spelling (``app/helpers/locations.py``);
* optionally (``--collapse-identical``) units with the same type, place, size, bedrooms and price are
  merged into one row that records ``units_available``. The data holds many identical units of one
  development, so a search would otherwise return the same line several times.

Rows that are only unusual (price over 200 M EGP, area over 2000 m²) are reported, never dropped.
The output name must not contain ``test_``, ``demo`` or ``sample``: such files are treated as
demo data and hidden from customers (``DEMO_SOURCE_MARKERS``).

    python scripts/clean_listings.py data/raw/properties/test_properties.csv \\
        -o data/raw/properties/listings_clean.csv [--collapse-identical]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.helpers.locations import canonical_district  # noqa: E402

MIN_PRICE = 100_000
MIN_AREA = 20
MIN_PRICE_PER_SQM = 3_000
MAX_PRICE_PER_SQM = 500_000
REVIEW_PRICE = 200_000_000
REVIEW_AREA = 2_000
# A room or a plot is allowed to be small / cheap per m²; every other type must pass the checks.
LENIENT_TYPES = {"Room", "Residential Land", "Land"}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def problem(meta: dict) -> str | None:
    """Why this row cannot be a real listing, or None."""
    price, area = _num(meta.get("price_egp")), _num(meta.get("area_sqm"))
    lenient = meta.get("type") in LENIENT_TYPES
    if price is None:
        return "no price"
    if price < MIN_PRICE:
        return f"price under {MIN_PRICE:,}"
    if area is not None and area < MIN_AREA and not lenient:
        return f"area under {MIN_AREA} m²"
    if area and not lenient and not (MIN_PRICE_PER_SQM <= price / area <= MAX_PRICE_PER_SQM):
        return "price per m² outside the market range"
    return None


def clean(rows: list[dict], collapse: bool) -> tuple[list[dict], Counter, Counter]:
    dropped: Counter = Counter()
    review: Counter = Counter()
    kept: list[dict] = []
    groups: dict[tuple, dict] = {}
    for row in rows:
        try:
            meta = json.loads(row.get("metadata") or "{}")
        except ValueError:
            dropped["metadata is not valid JSON"] += 1
            continue
        reason = problem(meta)
        if reason:
            dropped[reason] += 1
            continue
        if _num(meta.get("price_egp")) > REVIEW_PRICE:
            review[f"price over {REVIEW_PRICE:,} EGP"] += 1
        if (_num(meta.get("area_sqm")) or 0) > REVIEW_AREA:
            review[f"area over {REVIEW_AREA:,} m²"] += 1
        if meta.get("location_district"):
            meta["location_district"] = canonical_district(meta["location_district"])
        key = None
        if collapse:
            key = tuple(meta.get(k) for k in ("type", "location_district", "location_area", "area_sqm", "beds", "price_egp"))
            if key in groups:
                groups[key]["_meta"]["units_available"] = groups[key]["_meta"].get("units_available", 1) + 1
                continue
        row = dict(row)
        row["_meta"] = meta
        kept.append(row)
        if collapse:
            groups[key] = row
    for row in kept:
        row["metadata"] = json.dumps(row.pop("_meta"), ensure_ascii=False)
    return kept, dropped, review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--collapse-identical", action="store_true")
    args = parser.parse_args()

    if any(marker in args.output.name.lower() for marker in ("test_", "demo", "sample")):
        parser.error("the output name contains test_/demo/sample: such files are hidden from customers as demo data")
    csv.field_size_limit(10**9)
    with open(args.source, newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or ["id", "text", "metadata"]
        rows = list(reader)

    kept, dropped, review = clean(rows, args.collapse_identical)
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"read {len(rows):,} rows -> wrote {len(kept):,} to {args.output}")
    for reason, count in dropped.most_common():
        print(f"  dropped {count:>6,}  {reason}")
    for reason, count in review.most_common():
        print(f"  review  {count:>6,}  {reason} (kept)")
    if args.collapse_identical:
        merged = sum(1 for row in kept if json.loads(row["metadata"]).get("units_available"))
        print(f"  {merged:,} rows now stand for several identical units (units_available)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
