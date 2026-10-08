from datetime import date, timedelta

from studybuddy.streaks import current_streak

TODAY = date(2026, 10, 8)


def days_ago(*ns):
    return [TODAY - timedelta(days=n) for n in ns]


def test_no_activity():
    assert current_streak([], TODAY) == 0


def test_streak_including_today():
    assert current_streak(days_ago(0, 1, 2), TODAY) == 3


def test_streak_survives_until_end_of_next_day():
    assert current_streak(days_ago(1, 2), TODAY) == 2


def test_gap_breaks_the_streak():
    assert current_streak(days_ago(0, 1, 3, 4), TODAY) == 2


def test_old_activity_does_not_count():
    assert current_streak(days_ago(2, 3), TODAY) == 0


def test_duplicate_sessions_on_one_day():
    assert current_streak(days_ago(0, 0, 1), TODAY) == 2
