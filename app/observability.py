"""Observability: structured logs, Prometheus metrics and request correlation.

Three pillars so the service can be run and debugged in production:

* **Structured logging** — every log line is JSON and carries a ``request_id``
  so logs from the API and the worker can be stitched together per request.
* **Metrics** — Prometheus counters/histograms for HTTP requests and Celery
  tasks, scraped from ``/metrics`` (API) and a small metrics server (worker).
* **Correlation** — an ``X-Request-ID`` is generated (or accepted) at the edge,
  bound to a context variable, propagated into the Celery task, and echoed back
  to the client, giving an end-to-end handle on a single request.

Full distributed tracing (OpenTelemetry spans across API → broker → worker →
model call) is intentionally left to Ticket 16; this ticket delivers the logs
and metrics you need to operate and debug the service day to day.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

# ---------------------------------------------------------------------------
# Correlation id
# ---------------------------------------------------------------------------
# One id per request, carried through logs and into the Celery task. Defaults
# to "-" when there is no active request (e.g. worker startup logs).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Structured (JSON) logging
# ---------------------------------------------------------------------------
class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON with the active request id.

    Arbitrary structured fields can be attached with
    ``logger.info("msg", extra={"extra_fields": {"rid": rid}})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send all logs through a single JSON handler on the root logger."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Let framework loggers bubble up to the root handler instead of using
    # their own plain-text handlers, so everything comes out as JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)
CELERY_TASKS = Counter(
    "celery_tasks_total",
    "Celery tasks processed, by terminal state.",
    ["task", "status"],
)
CELERY_TASK_LATENCY = Histogram(
    "celery_task_duration_seconds",
    "Celery task execution time in seconds.",
    ["task"],
)


def render_metrics() -> tuple[bytes, str]:
    """Return the Prometheus exposition payload and its content type.

    When ``PROMETHEUS_MULTIPROC_DIR`` is set (the Celery worker runs several
    child processes), metrics are aggregated across all of them; otherwise the
    default in-process registry is used.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()
    return data, CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------
async def request_context_middleware(request, call_next):
    """Assign/propagate a request id, time the request, log it and count it."""
    incoming = request.headers.get("x-request-id")
    request_id = incoming or new_request_id()
    token = request_id_var.set(request_id)

    start = time.perf_counter()
    logger = logging.getLogger("docintel.access")
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start
        # Use the route template (not the raw path) to keep label cardinality
        # bounded — e.g. "/results/{rid}" rather than one label per id.
        path = _route_path(request)
        HTTP_REQUESTS.labels(request.method, path, "500").inc()
        HTTP_LATENCY.labels(request.method, path).observe(duration)
        logger.exception(
            "request failed",
            extra={"extra_fields": {
                "method": request.method,
                "path": path,
                "duration_ms": round(duration * 1000, 2),
            }},
        )
        request_id_var.reset(token)
        raise

    duration = time.perf_counter() - start
    path = _route_path(request)
    HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    HTTP_LATENCY.labels(request.method, path).observe(duration)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request completed",
        extra={"extra_fields": {
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration_ms": round(duration * 1000, 2),
        }},
    )
    request_id_var.reset(token)
    return response


def _route_path(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


# ---------------------------------------------------------------------------
# Celery worker instrumentation
# ---------------------------------------------------------------------------
_task_starts: dict[str, float] = {}


def setup_worker_observability() -> None:
    """Wire JSON logging, task metrics and a metrics server into the worker.

    Called from ``celery_app`` so both the worker and any imports get the same
    structured logging.
    """
    configure_logging()

    from celery.signals import task_failure, task_postrun, task_prerun, worker_ready

    @task_prerun.connect
    def _on_task_prerun(task_id=None, task=None, **_):  # noqa: ANN001
        _task_starts[task_id] = time.perf_counter()

    @task_postrun.connect
    def _on_task_postrun(task_id=None, task=None, state=None, **_):  # noqa: ANN001
        start = _task_starts.pop(task_id, None)
        name = getattr(task, "name", "unknown")
        if start is not None:
            CELERY_TASK_LATENCY.labels(name).observe(time.perf_counter() - start)
        CELERY_TASKS.labels(name, state or "UNKNOWN").inc()

    @task_failure.connect
    def _on_task_failure(sender=None, **_):  # noqa: ANN001
        name = getattr(sender, "name", "unknown")
        logging.getLogger("docintel.tasks").error(
            "task raised", extra={"extra_fields": {"task": name}}
        )

    @worker_ready.connect
    def _on_worker_ready(**_):  # noqa: ANN001
        _start_worker_metrics_server()


def _start_worker_metrics_server() -> None:
    """Expose the worker's metrics over HTTP for Prometheus to scrape."""
    from prometheus_client import start_http_server

    port = int(os.environ.get("WORKER_METRICS_PORT", "9100"))
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        os.makedirs(multiproc_dir, exist_ok=True)
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(port, registry=registry)
    else:
        start_http_server(port)
    logging.getLogger("docintel").info(
        "worker metrics server started",
        extra={"extra_fields": {"port": port}},
    )
