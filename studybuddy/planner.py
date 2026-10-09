"""Study plan builder: spreads topics across days, weighted by difficulty."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .telemetry import log, meter, tracer

allocations = meter.create_counter(
    "studybuddy.planner.allocations", description="Topic time allocations computed"
)
minutes_per_topic = meter.create_histogram(
    "studybuddy.planner.minutes_per_topic", unit="min", description="Minutes given to a topic per day"
)


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
        if not topics:
            log.warning("empty study plan requested")
            return [{} for _ in range(days)]
        total_weight = sum(max(1, min(5, d)) for _, d in topics)
        plan: List[Dict[str, int]] = []
        # Build each day separately so per-day adjustments can be added later.
        for _ in range(days):
            day_plan: Dict[str, int] = {}
            for name, difficulty in topics:
                weight = max(1, min(5, difficulty))
                day_plan[name] = round(minutes_per_day * weight / total_weight)
                allocations.add(1)
                minutes_per_topic.record(day_plan[name])
            plan.append(day_plan)
        return plan
