"""Study streaks: consecutive days with at least one study session."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from .telemetry import tracer


def current_streak(study_days: Iterable[date], today: date) -> int:
    """Number of consecutive days, ending today or yesterday, with study activity.

    A streak survives until the end of the day after the last session, so a
    learner who studied yesterday but not yet today still has their streak.
    """
    with tracer.start_as_current_span("streaks.current") as span:
        days = set(study_days)
        if today in days:
            cursor = today
        elif today - timedelta(days=1) in days:
            cursor = today - timedelta(days=1)
        else:
            span.set_attribute("streaks.length", 0)
            return 0
        streak = 0
        while cursor in days:
            streak += 1
            cursor -= timedelta(days=1)
        span.set_attribute("streaks.length", streak)
        return streak
