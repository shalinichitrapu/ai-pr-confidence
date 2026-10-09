import pytest

from studybuddy.planner import build_study_plan


def test_harder_topics_get_more_time():
    plan = build_study_plan([("algebra", 4), ("spelling", 1)], days=2, minutes_per_day=50)
    assert len(plan) == 2
    assert plan[0] == {"algebra": 40, "spelling": 10}


def test_every_day_has_the_same_mix():
    plan = build_study_plan([("a", 2), ("b", 3)], days=3)
    assert plan[0] == plan[1] == plan[2]


def test_difficulty_is_clamped():
    plan = build_study_plan([("a", 99), ("b", -3)], days=1, minutes_per_day=60)
    assert plan[0] == {"a": 50, "b": 10}


def test_days_must_be_positive():
    with pytest.raises(ValueError):
        build_study_plan([("a", 1)], days=0)


def test_minutes_add_up_exactly():
    plan = build_study_plan([("a", 1), ("b", 1), ("c", 1)], days=1, minutes_per_day=100)
    assert sum(plan[0].values()) == 100
