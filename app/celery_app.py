"""Celery application: the task queue that runs heavy work off the request path.

The API process *enqueues* jobs here; a separate worker process (see the
`worker` service in docker-compose) consumes and runs them. Redis is used as
both the broker (the queue itself) and the result backend.
"""

from celery import Celery

from app.config import settings
from app.observability import setup_worker_observability
from app.tracing import configure_tracing

celery_app = Celery(
    "docintel",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    # Only acknowledge a job once it has finished, not when it is picked up.
    # If a worker crashes mid-job, the job is redelivered instead of lost.
    task_acks_late=True,
    # acks_late alone does not cover a worker that is SIGKILLed: without this
    # the job is silently dropped rather than requeued.
    task_reject_on_worker_lost=True,
    # How long the broker waits for an ack before handing the job to someone
    # else. Must exceed the slowest task or work gets run twice needlessly.
    broker_transport_options={"visibility_timeout": 3600},
    # Fetch one job at a time per worker slot so slow jobs don't hog a worker
    # that has already prefetched a backlog. Also keeps queue depth honest,
    # which is what the autoscaler scales on.
    worker_prefetch_multiplier=1,
    # Report a "started" state so clients/monitoring can see work in progress.
    task_track_started=True,
    task_time_limit=600,
    task_soft_time_limit=540,
)

# Structured logging, task metrics and the worker metrics server.
setup_worker_observability()
configure_tracing("docintel-worker")
