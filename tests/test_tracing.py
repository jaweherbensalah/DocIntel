"""One request should produce one trace covering API, queue, worker and model."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_docintel.db")
os.environ.setdefault("API_KEY", "test-key")

from pathlib import Path

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import limits
from app.admin import create_client
from app.celery_app import celery_app
from app.config import settings
from app.main import app

settings.llm_latency = 0
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    limits.use_client(fakeredis.FakeStrictRedis())
    return exporter


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def api_key():
    return create_client("tracing", budget_cents=1_000_000, rate_per_minute=1000)


def test_extract_produces_one_trace_through_queue_and_model(client, spans, api_key):
    spans.clear()

    r = client.post(
        "/extract",
        headers={"x-api-key": api_key},
        files={"file": ("cv.txt", b"Trace Person\nPython, 4 years")},
    )
    assert r.status_code == 202

    recorded = spans.get_finished_spans()
    by_name = {s.name: s for s in recorded}

    assert "extract_profile" in by_name
    assert "llm.extract" in by_name
    http_span = next(s for s in recorded if s.name.startswith("POST"))

    # The whole journey shares one trace id: API span, queued task, model call.
    trace_ids = {
        http_span.context.trace_id,
        by_name["extract_profile"].context.trace_id,
        by_name["llm.extract"].context.trace_id,
    }
    assert len(trace_ids) == 1

    # And the model call is nested under the task, not floating beside it.
    assert (
        by_name["llm.extract"].parent.span_id
        == by_name["extract_profile"].context.span_id
    )


def test_incoming_traceparent_is_continued(client, spans, api_key):
    spans.clear()
    upstream = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    client.get(
        "/candidates", headers={"x-api-key": api_key, "traceparent": upstream}
    )

    http_span = next(s for s in spans.get_finished_spans() if s.name.startswith("GET"))
    assert format(http_span.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_span_records_the_route_template_not_the_id(client, spans, api_key):
    spans.clear()
    client.get("/results/does-not-exist", headers={"x-api-key": api_key})

    http_span = next(s for s in spans.get_finished_spans() if s.name.startswith("GET"))
    assert http_span.attributes["http.route"] == "/results/{rid}"
