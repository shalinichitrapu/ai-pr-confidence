"""Topic mastery report built from quiz history."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .telemetry import tracer

LEVELS = [(90, "mastered"), (70, "proficient"), (50, "developing")]


def mastery_level(percent: float) -> str:
    for threshold, label in LEVELS:
        if percent >= threshold:
            return label
    return "needs practice"


def mastery_report(history: List[Tuple[str, float]], recent: int = 3) -> Dict[str, Dict[str, object]]:
    """Summarize (topic, percent) quiz results into a mastery level per topic.

    Only the most recent `recent` quizzes per topic count, so the report
    reflects current understanding rather than early attempts.
    """
    with tracer.start_as_current_span("mastery.report") as span:
        by_topic: Dict[str, List[float]] = {}
        for topic, percent in history:
            by_topic.setdefault(topic, []).append(percent)
        report: Dict[str, Dict[str, object]] = {}
        for topic, scores in by_topic.items():
            window = scores[-recent:]
            average = round(sum(window) / len(window), 1)
            trend = "flat"
            if len(window) >= 2:
                if window[-1] > window[0]:
                    trend = "improving"
                elif window[-1] < window[0]:
                    trend = "slipping"
            report[topic] = {
                "average": average,
                "level": mastery_level(average),
                "trend": trend,
                "quizzes": len(scores),
            }
        span.set_attribute("mastery.topics", len(report))
        return report
