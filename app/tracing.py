"""Distributed tracing across the API, the queue and the worker.

Instrumentation is always present but inert unless OTEL_ENABLED is set: with no
provider configured the OpenTelemetry API hands back non-recording spans, so
the cost when it is off is a few attribute assignments that go nowhere.

Ticket 7 gave every request an id that ties its logs together. That stops at
"which lines belong to this request". This adds where the time went.
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.config import settings

_propagator = TraceContextTextMapPropagator()


def configure_tracing(service_name: str) -> None:
    if not settings.otel_enabled or trace.get_tracer_provider().__class__ is TracerProvider:
        return

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(_exporter()))
    trace.set_tracer_provider(provider)
    logging.getLogger("docintel").info(
        "tracing enabled",
        extra={"extra_fields": {"service": service_name, "exporter": settings.otel_exporter}},
    )


def _exporter():
    if settings.otel_exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=settings.otel_endpoint or None)
    return ConsoleSpanExporter()


def tracer():
    return trace.get_tracer("docintel")


def carrier() -> dict:
    """W3C traceparent for the active span, to travel with a queued job."""
    out: dict = {}
    _propagator.inject(out)
    return out


def context_from(incoming: dict):
    return _propagator.extract(incoming or {})


def current_trace_id() -> str:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return "-"
    return format(ctx.trace_id, "032x")
