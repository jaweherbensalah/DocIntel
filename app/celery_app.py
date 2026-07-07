"""Celery application: the task queue that runs heavy work off the request path.

The API process *enqueues* jobs here; a separate worker process (see the
`worker` service in docker-compose) consumes and runs them. Redis is used as
both the broker (the queue itself) and the result backend.
"""

from celery import Celery

from app.config import settings

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
    # Fetch one job at a time per worker slot so slow jobs don't hog a worker
    # that has already prefetched a backlog.
    worker_prefetch_multiplier=1,
    # Report a "started" state so clients/monitoring can see work in progress.
    task_track_started=True,
)
