"""Quiz grading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .telemetry import log, meter, tracer

answers_graded = meter.create_counter(
    "studybuddy.quiz.answers", description="Questions graded, by result"
)
quiz_score = meter.create_histogram("studybuddy.quiz.score", unit="%", description="Quiz scores")


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
                answers_graded.add(1, {"result": "correct"})
            else:
                missed.append(question)
                answers_graded.add(1, {"result": "missed"})
        extra = set(answers) - set(key)
        if extra:
            log.warning("ignoring %d answers for unknown questions", len(extra))
        span.set_attribute("quiz.correct", correct)
        result = QuizResult(correct=correct, total=len(key), missed=missed)
        if key:
            quiz_score.record(result.percent)
        return result
