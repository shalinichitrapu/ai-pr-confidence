"""Study plan builder: spreads topics across days, weighted by difficulty."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .telemetry import tracer


def build_study_plan(
    topics: List[Tuple[str, int]], days: int, minutes_per_day: int = 60
) -> List[Dict[str, int]]:
    """Return one dict per day mapping topic -> minutes.

    topics is a list of (name, difficulty) with difficulty 1-5. Harder topics
    get proportionally more time. Each day gets the same mix so that topics
    are practiced repeatedly rather than crammed.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    with tracer.start_as_current_span("planner.build") as span:
        span.set_attribute("planner.topics", len(topics))
        span.set_attribute("planner.days", days)
        total_weight = sum(d for _, d in topics)
        day_plan: Dict[str, int] = {}
        for name, difficulty in topics:
            day_plan[name] = minutes_per_day * difficulty // total_weight
        # Give the minutes lost to rounding down to the hardest topic so each
        # day adds up to exactly minutes_per_day.
        leftover = minutes_per_day - sum(day_plan.values())
        hardest = max(topics, key=lambda t: t[1])[0]
        day_plan[hardest] += leftover
        return [dict(day_plan) for _ in range(days)]
