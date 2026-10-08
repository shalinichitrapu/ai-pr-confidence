"""Flashcard scheduling using a simplified SM-2 spaced-repetition algorithm."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .telemetry import tracer

MIN_EASE = 1.3


@dataclass(frozen=True)
class Card:
    front: str
    back: str
    interval_days: int = 0
    repetitions: int = 0
    ease: float = 2.5


def next_review(card: Card, quality: int) -> Card:
    """Return the card rescheduled after a review.

    quality is the learner's self-rating from 0 (forgot) to 5 (perfect).
    """
    if not 0 <= quality <= 5:
        raise ValueError("quality must be between 0 and 5")
    with tracer.start_as_current_span("srs.next_review") as span:
        span.set_attribute("srs.quality", quality)
        if quality < 3:
            # Forgotten: start the card over, but keep its ease.
            updated = replace(card, repetitions=0, interval_days=1)
        else:
            reps = card.repetitions + 1
            if reps == 1:
                interval = 1
            elif reps == 2:
                interval = 6
            else:
                interval = round(card.interval_days * card.ease)
            ease = card.ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            updated = replace(
                card,
                repetitions=reps,
                interval_days=interval,
                ease=max(MIN_EASE, round(ease, 2)),
            )
        span.set_attribute("srs.interval_days", updated.interval_days)
        return updated
