"""Shared instrumentation handles.

The app only depends on the OpenTelemetry *API*, for traces and metrics.
Whoever runs the app decides where telemetry goes (the confidence agent installs an in-memory SDK during
test runs; production could export to Datadog, Jaeger, etc.).
"""

import logging

from opentelemetry import metrics, trace

tracer = trace.get_tracer("studybuddy")
meter = metrics.get_meter("studybuddy")
log = logging.getLogger("studybuddy")
