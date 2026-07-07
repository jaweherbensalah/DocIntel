"""Background tasks run by the Celery worker."""

import json
import logging
import time

from app.celery_app import celery_app
from app.llm import get_provider
from app.models import Result
from app.observability import request_id_var
from app.sync_db import SyncSessionLocal

logger = logging.getLogger("docintel.tasks")


@celery_app.task(name="extract_profile", bind=True, max_retries=3, default_retry_delay=5)
def extract_profile(self, rid: str, text: str, request_id: str = "-") -> None:
    """Run the (slow) extraction off the request path and store the result.

    The API has already created a ``Result`` row with status ``pending``; this
    task fills in the profile and flips the status to ``done`` (or ``failed``).
    ``request_id`` is the id of the originating HTTP request, bound to the log
    context so worker logs correlate with the API logs for that request.
    """
    request_id_var.set(request_id)
    started = time.perf_counter()
    logger.info("extraction started", extra={"extra_fields": {"rid": rid}})
    with SyncSessionLocal() as session:
        obj = session.get(Result, rid)
        if obj is None:
            logger.warning("result %s no longer exists; skipping", rid)
            return

        try:
            profile = get_provider().extract(text)
        except Exception as exc:
            obj.status = "failed"
            session.commit()
            logger.exception("extraction failed for %s", rid)
            raise self.retry(exc=exc)

        obj.payload = json.dumps(profile)
        obj.status = "done"
        session.commit()
        logger.info(
            "extraction done",
            extra={"extra_fields": {
                "rid": rid,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }},
        )
