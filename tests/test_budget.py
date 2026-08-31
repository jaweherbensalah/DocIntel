"""Budget and rate limit enforcement, including under concurrency.

The interesting cases are the concurrent ones: the ticket is not "count
requests", it is "a client must not exceed their limits by sending many
requests at the same time".
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app import budget, limits
from app.admin import hash_key
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'budget.db'}"
    monkeypatch.setattr(settings, "database_url", url)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    return create_engine(url)


@pytest.fixture()
def fake_redis():
    server = fakeredis.FakeStrictRedis()
    limits.use_client(server)
    return server


def make_client(engine, budget_cents=1000, rate=60):
    client_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO clients "
                "(id, name, api_key_hash, monthly_budget_cents, rate_limit_per_minute, active) "
                "VALUES (:id, 'test', :hash, :budget, :rate, 1)"
            ),
            {
                "id": client_id,
                "hash": hash_key(client_id),
                "budget": budget_cents,
                "rate": rate,
            },
        )
    return client_id


def reserve_once(engine, client_id, limit_cents, cost):
    """The synchronous equivalent of budget.reserve, for thread testing."""
    period = budget.current_period()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_periods (client_id, period, reserved_cents, spent_cents) "
                "VALUES (:c, :p, 0, 0) ON CONFLICT (client_id, period) DO NOTHING"
            ),
            {"c": client_id, "p": period},
        )
    with engine.begin() as conn:
        updated = conn.execute(
            text(
                "UPDATE budget_periods SET reserved_cents = reserved_cents + :cost "
                "WHERE client_id = :c AND period = :p "
                "AND spent_cents + reserved_cents + :cost <= :limit"
            ),
            {"c": client_id, "p": period, "cost": cost, "limit": limit_cents},
        )
        return updated.rowcount == 1


def test_reservation_stops_at_the_budget(db):
    client_id = make_client(db, budget_cents=100)
    granted = sum(reserve_once(db, client_id, 100, 10) for _ in range(15))
    assert granted == 10


def test_concurrent_reservations_never_overspend(db):
    """Twenty threads race for a budget that only covers ten of them."""
    client_id = make_client(db, budget_cents=100)

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(
            pool.map(lambda _: reserve_once(db, client_id, 100, 10), range(20))
        )

    assert sum(outcomes) == 10

    with db.connect() as conn:
        reserved = conn.execute(
            text("SELECT reserved_cents FROM budget_periods WHERE client_id = :c"),
            {"c": client_id},
        ).scalar_one()
    assert reserved == 100


def test_settle_replaces_the_reservation_with_actual_cost(db):
    from app.sync_db import SyncSessionLocal

    client_id = make_client(db, budget_cents=1000)
    period = budget.current_period()
    event_id = uuid.uuid4().hex

    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_periods (client_id, period, reserved_cents, spent_cents) "
                "VALUES (:c, :p, 50, 0)"
            ),
            {"c": client_id, "p": period},
        )
        conn.execute(
            text(
                "INSERT INTO usage_events "
                "(id, client_id, period, operation, estimated_cents, state) "
                "VALUES (:id, :c, :p, 'extract', 50, 'reserved')"
            ),
            {"id": event_id, "c": client_id, "p": period},
        )

    engine_session = SyncSessionLocal
    with engine_session(bind=db) as session:
        assert budget.settle_sync(session, event_id, 30) is True
        # A redelivered task must not charge twice.
        assert budget.settle_sync(session, event_id, 30) is False

    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT reserved_cents, spent_cents FROM budget_periods "
                "WHERE client_id = :c"
            ),
            {"c": client_id},
        ).one()
    assert row.reserved_cents == 0
    assert row.spent_cents == 30


def test_rate_limiter_allows_exactly_the_limit(fake_redis):
    client_id = uuid.uuid4().hex
    decisions = [limits.check(client_id, 5) for _ in range(8)]
    assert sum(d.allowed for d in decisions) == 5
    assert decisions[-1].retry_after_seconds >= 1


def test_rate_limiter_is_atomic_under_concurrency(fake_redis):
    """Fifty simultaneous requests against a limit of ten."""
    client_id = uuid.uuid4().hex

    with ThreadPoolExecutor(max_workers=50) as pool:
        decisions = list(pool.map(lambda _: limits.check(client_id, 10), range(50)))

    assert sum(d.allowed for d in decisions) == 10


def test_rate_limit_is_per_client(fake_redis):
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    for _ in range(3):
        limits.check(a, 3)
    assert limits.check(a, 3).allowed is False
    assert limits.check(b, 3).allowed is True
