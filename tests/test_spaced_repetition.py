import pytest

from studybuddy.spaced_repetition import MIN_EASE, Card, next_review


def card():
    return Card(front="mitochondria", back="powerhouse of the cell")


def test_first_two_reviews_use_fixed_intervals():
    c = next_review(card(), 4)
    assert (c.repetitions, c.interval_days) == (1, 1)
    c = next_review(c, 4)
    assert (c.repetitions, c.interval_days) == (2, 6)


def test_third_review_multiplies_by_ease():
    c = Card("a", "b", interval_days=6, repetitions=2, ease=2.5)
    assert next_review(c, 5).interval_days == 15


def test_forgetting_resets_the_card():
    c = Card("a", "b", interval_days=30, repetitions=5, ease=2.2)
    after = next_review(c, 1)
    assert (after.repetitions, after.interval_days, after.ease) == (0, 1, 2.2)


def test_ease_never_drops_below_minimum():
    c = Card("a", "b", ease=MIN_EASE)
    assert next_review(c, 3).ease == MIN_EASE


def test_invalid_quality_is_rejected():
    with pytest.raises(ValueError):
        next_review(card(), 6)
