"""Redelivery safety.

Late acks give at-least-once delivery, so an interrupted job comes back. That
is only safe if running it twice is indistinguishable from running it once.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_docintel.db")
os.environ.setdefault("API_KEY", "test-key")

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app import budget
from app.admin import create_client, hash_key
from app.celery_app import celery_app
from app.config import settings
from app.sync_db import sync_engine
from app.tasks import extract_profile

settings.llm_latency = 0
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def schema():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def queued_job():
    """A pending result plus its budget reservation, as the API would create."""
    key = create_client("redelivery", budget_cents=1_000_000, rate_per_minute=100)
    with sync_engine.connect() as conn:
        # Looked up by key hash, not by "most recent": SQLite timestamps are
        # second-granular, so ordering by created_at picks an arbitrary client.
        cid = conn.execute(
            text("SELECT id FROM clients WHERE api_key_hash = :h"),
            {"h": hash_key(key)},
        ).scalar_one()

    rid = uuid.uuid4().hex
    event_id = uuid.uuid4().hex
    period = budget.current_period()

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO results (id, kind, client_id, status) "
                "VALUES (:rid, 'extract', :cid, 'pending')"
            ),
            {"rid": rid, "cid": cid},
        )
        conn.execute(
            text(
                "INSERT INTO budget_periods (client_id, period, reserved_cents, spent_cents) "
                "VALUES (:cid, :p, 40, 0) ON CONFLICT (client_id, period) DO NOTHING"
            ),
            {"cid": cid, "p": period},
        )
        conn.execute(
            text(
                "INSERT INTO usage_events "
                "(id, client_id, period, operation, estimated_cents, state) "
                "VALUES (:eid, :cid, :p, 'extract', 40, 'reserved')"
            ),
            {"eid": event_id, "cid": cid, "p": period},
        )
    return rid, event_id, cid


def test_redelivered_job_does_not_duplicate_the_profile():
    rid, event_id, _ = queued_job()
    body = "Redelivery Person\nPython and Docker, 6 years"

    extract_profile.run(rid, body, "-", event_id)
    extract_profile.run(rid, body, "-", event_id)

    with sync_engine.connect() as conn:
        profiles = conn.execute(
            text("SELECT COUNT(*) FROM profiles WHERE result_id = :rid"), {"rid": rid}
        ).scalar_one()
        skills = conn.execute(
            text(
                "SELECT COUNT(*) FROM profile_skills ps "
                "JOIN profiles p ON p.id = ps.profile_id WHERE p.result_id = :rid"
            ),
            {"rid": rid},
        ).scalar_one()

    assert profiles == 1
    assert skills > 0


def test_redelivered_job_is_charged_once():
    rid, event_id, client_id = queued_job()
    body = "Billing Person\nPython, 2 years"

    extract_profile.run(rid, body, "-", event_id)
    extract_profile.run(rid, body, "-", event_id)

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, actual_cents FROM usage_events WHERE id = :eid"),
            {"eid": event_id},
        ).one()
        period = conn.execute(
            text(
                "SELECT reserved_cents, spent_cents FROM budget_periods "
                "WHERE client_id = :cid"
            ),
            {"cid": client_id},
        ).one()

    assert row.state == "settled"
    assert period.reserved_cents == 0
    assert period.spent_cents == row.actual_cents


def test_job_for_a_deleted_result_releases_its_reservation():
    rid, event_id, client_id = queued_job()

    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM results WHERE id = :rid"), {"rid": rid})

    extract_profile.run(rid, "anything", "-", event_id)

    with sync_engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM usage_events WHERE id = :eid"), {"eid": event_id}
        ).scalar_one()
        reserved = conn.execute(
            text("SELECT reserved_cents FROM budget_periods WHERE client_id = :cid"),
            {"cid": client_id},
        ).scalar_one()

    assert state == "released"
    assert reserved == 0


def test_queue_settings_make_redelivery_possible():
    conf = celery_app.conf
    assert conf.task_acks_late is True
    # Without this a SIGKILLed worker's job is dropped rather than requeued.
    assert conf.task_reject_on_worker_lost is True
    # Queue depth must reflect real backlog for the autoscaler to be right.
    assert conf.worker_prefetch_multiplier == 1
    assert conf.broker_transport_options["visibility_timeout"] > conf.task_time_limit
