"""Misreadings found by running realistic Egyptian-Arabic / English / Arabizi messages through the NLU."""

from __future__ import annotations

from datetime import date

from app.agents.understanding import extract_date, extract_qualification, extract_requirements, extract_time


def test_not_more_than_is_a_ceiling_not_a_floor():
    assert extract_requirements("مش اكتر من 6 مليون") == {"max_price": 6_000_000.0}
    assert extract_requirements("not more than 6 million") == {"max_price": 6_000_000.0}
    # the genuine "more than" still sets a floor
    assert extract_requirements("اكتر من 6 مليون")["min_price"] == 6_000_000.0
    assert extract_requirements("6 مليون على الاقل")["min_price"] == 6_000_000.0


def test_at_least_n_bedrooms_is_a_minimum_not_an_exact_count():
    assert extract_requirements("3 غرف على الاقل") == {"min_bedrooms": 3}
    assert extract_requirements("at least 3 bedrooms") == {"min_bedrooms": 3}
    assert extract_requirements("اكتر من 3 غرف") == {"min_bedrooms": 4}
    assert extract_requirements("3 غرف") == {"bedrooms": 3}
    assert extract_requirements("مش اكتر من 3 غرف") == {}  # a bedroom ceiling is not modelled: say nothing, never a wrong exact count


def test_a_name_stops_before_the_rest_of_the_sentence():
    assert extract_qualification("اسمي احمد وعايز شقة")["name"] == "احمد"
    assert extract_qualification("my name is Sara and I want a villa")["name"] == "Sara"
    assert extract_qualification("اسمي محمد علي")["name"] == "محمد علي"


def test_common_arabizi_is_understood():
    assert extract_requirements("ayez sha2a fel tagamo3 3 oda") == {"property_type": "Apartment", "district": "New Cairo", "bedrooms": 3}


def test_five_million_is_not_five_pm_and_the_maximum_is_not_sunday():
    today = date(2026, 11, 3)
    assert extract_time("ميزانيتي 5 مليون") is None
    assert extract_time("150 متر") is None
    assert extract_date("الحد الاقصى 5 مليون", today) is None
    assert extract_date("الاربع غرف", today) is None
    assert extract_date("الحد الجاي", today) == date(2026, 11, 8)  # a real Sunday still works


def test_noon_hours_are_afternoon():
    assert extract_time("الساعة 1 الضهر") == (13, 0)
    assert extract_time("12 الضهر") == (12, 0)
    assert extract_time("الخميس القادم الساعة 5") == (17, 0)
    assert extract_time("10am") == (10, 0)
