"""StudyBuddy: a tiny study helper used as the demo app for the AI PR confidence agent.

It grades quizzes, schedules flashcard reviews (spaced repetition) and builds
study plans. Every function is instrumented with OpenTelemetry spans and
standard Python logging, which is what the confidence agent reads.
"""

__version__ = "0.1.0"
