"""One place-name table for ingestion and the assistant (app/helpers/locations.py)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.agents.understanding import extract_requirements
from app.helpers.locations import KNOWN_DISTRICTS, RAW_SPELLINGS, canonical_district, district_label, match_district

PROPERTIES = Path(__file__).resolve().parents[2] / "data" / "raw" / "properties"


def _data_districts() -> set[str]:
    csv.field_size_limit(10**9)
    found: set[str] = set()
    for path in PROPERTIES.glob("*.csv"):
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                try:
                    district = json.loads(row.get("metadata") or "{}").get("location_district")
                except ValueError:
                    continue
                if district:
                    found.add(district.strip())
    return found


def test_every_district_in_the_data_files_is_in_the_table():
    unknown = {d for d in _data_districts() if d not in KNOWN_DISTRICTS and d not in RAW_SPELLINGS}
    assert not unknown, f"add these districts to app/helpers/locations.py: {sorted(unknown)}"


def test_stored_spellings_collapse_to_one_canonical_name():
    for raw, expected in {
        "AinSukhna": "Ain Sukhna",
        "Ain Sokhna": "Ain Sukhna",
        "New Capital": "New Capital City",
        "Heliopolis": "Heliopolis",
        "New Heliopolis": "New Heliopolis",
        "  Madinaty ": "Madinaty",
        "Some Unknown Place": "Some Unknown Place",
    }.items():
        assert canonical_district(raw) == expected, raw
    assert canonical_district(None) is None and canonical_district("  ") is None


def test_every_canonical_name_is_stable():
    for name in KNOWN_DISTRICTS:
        assert canonical_district(name) == name


def test_customer_wording_becomes_the_stored_spelling():
    for text, expected in {
        "في نيو كايرو 3 غرف": "New Cairo",
        "عايز شقة في التجمع الخامس": "New Cairo",
        "5th settlement": "New Cairo",
        "شقة في New Capital City": "New Capital City",
        "العاصمة الإدارية": "New Capital City",
        "Ain Sokhna": "Ain Sukhna",
        "العين السخنة": "Ain Sukhna",
        "الساحل الشمالي": "North Coast",
        "مدينتي": "Madinaty",
        "الشروق": "Shorouk City",
        "المعادي": "Maadi",
        "فيلا في الشيخ زايد": "Sheikh Zayed",
        "مصر الجديدة": "Heliopolis",
        "هليوبوليس الجديدة": "New Heliopolis",
        "new heliopolis": "New Heliopolis",
        "حدائق اكتوبر": "Hadayek October",
        "اكتوبر": "6th of October",
        "مدينة المستقبل": "Mostakbal City",
    }.items():
        assert extract_requirements(text).get("district") == expected, text


def test_ordinary_words_and_unclear_messages_name_no_district():
    for text in ("ناوي اشتري في المستقبل", "تجمعات سكنية", "القاهرة", "عايز شقة", "التجمع او الشيخ زايد"):
        assert "district" not in extract_requirements(text), text


def test_arabic_label_for_customers():
    assert district_label("New Cairo", "ar") == "التجمع (القاهرة الجديدة)"
    assert district_label("New Cairo", "en") == "New Cairo"
    assert district_label("Unlisted Place", "ar") == "Unlisted Place"
