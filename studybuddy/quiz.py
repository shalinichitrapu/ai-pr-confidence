"""Quiz grading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .telemetry import log, tracer


@dataclass
class QuizResult:
    correct: int
    total: int
    missed: List[str] = field(default_factory=list)

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.correct / self.total, 1)


def grade_quiz(answers: Dict[str, Any], key: Dict[str, Any]) -> QuizResult:
    """Grade a student's answers against an answer key.

    Questions the student skipped count as missed. Answers to questions that
    are not in the key are ignored.
    """
    with tracer.start_as_current_span("quiz.grade") as span:
        span.set_attribute("quiz.questions", len(key))
        correct = 0
        missed: List[str] = []
        for question, expected in key.items():
            if question in answers and answers[question] == expected:
                correct += 1
            else:
                missed.append(question)
        extra = set(answers) - set(key)
        if extra:
            log.warning("ignoring %d answers for unknown questions", len(extra))
        span.set_attribute("quiz.correct", correct)
        return QuizResult(correct=correct, total=len(key), missed=missed)
