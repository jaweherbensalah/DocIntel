"""Background tasks run by the Celery worker."""

import json
import logging
import time

from sqlalchemy import select

from app.budget import estimate_cents, release_sync, settle_sync
from app.celery_app import celery_app
from app.llm import get_provider
from app.models import Profile, Result
from app.observability import request_id_var
from app.persistence import build_profile
from app.sync_db import SyncSessionLocal

logger = logging.getLogger("docintel.tasks")


@celery_app.task(name="extract_profile", bind=True, max_retries=3, default_retry_delay=5)
def extract_profile(
    self, rid: str, text: str, request_id: str = "-", event_id: str = ""
) -> None:
    """Run the (slow) extraction off the request path and store the result.

    ``event_id`` is the budget reservation the API took before enqueueing; it
    is settled at the real cost here, or released if the work never happened.
    """
    request_id_var.set(request_id)
    started = time.perf_counter()
    logger.info("extraction started", extra={"extra_fields": {"rid": rid}})
    with SyncSessionLocal() as session:
        obj = session.get(Result, rid)
        if obj is None:
            logger.warning("result %s no longer exists; skipping", rid)
            if event_id:
                release_sync(session, event_id)
            return

        try:
            profile = get_provider().extract(text)
        except Exception as exc:
            obj.status = "failed"
            session.commit()
            logger.exception("extraction failed for %s", rid)
            if self.request.retries >= self.max_retries and event_id:
                release_sync(session, event_id)
            raise self.retry(exc=exc)

        obj.payload = json.dumps(profile)
        obj.status = "done"
        # acks_late can redeliver a job that already committed
        already_stored = session.scalar(
            select(Profile.id).where(Profile.result_id == rid)
        )
        if already_stored is None:
            session.add(build_profile(rid, profile))
        session.commit()

        if event_id:
            settle_sync(session, event_id, estimate_cents("extract", len(text)))

        logger.info(
            "extraction done",
            extra={"extra_fields": {
                "rid": rid,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }},
        )
