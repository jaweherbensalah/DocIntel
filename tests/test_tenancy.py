"""One client must never see or touch another's data."""

import os

# app.config.settings is built on first import, so the value here only takes
# effect if this module happens to be imported first; either way all API tests
# share one database file.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_docintel.db")
os.environ.setdefault("API_KEY", "test-key")

from pathlib import Path

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

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
def schema():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    limits.use_client(fakeredis.FakeStrictRedis())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def keys():
    return {
        "acme": create_client("acme", budget_cents=1_000_000, rate_per_minute=1000),
        "globex": create_client("globex", budget_cents=1_000_000, rate_per_minute=1000),
    }


def extract_as(client, key, body):
    r = client.post(
        "/extract", headers={"x-api-key": key}, files={"file": ("cv.txt", body)}
    )
    assert r.status_code == 202
    return r.json()["id"]


def test_one_tenant_cannot_read_anothers_result(client, keys):
    rid = extract_as(client, keys["acme"], b"Acme Person\nPython, 9 years")

    mine = client.get(f"/results/{rid}", headers={"x-api-key": keys["acme"]})
    assert mine.status_code == 200
    assert mine.json()["result"]["years_experience"] == 9

    theirs = client.get(f"/results/{rid}", headers={"x-api-key": keys["globex"]})
    assert theirs.status_code == 404


def test_candidate_search_is_scoped_to_the_caller(client, keys):
    extract_as(client, keys["acme"], b"Acme Candidate\nPython and Docker, 8 years")
    extract_as(client, keys["globex"], b"Globex Candidate\nPython and Docker, 8 years")

    def names_for(key):
        r = client.get(
            "/candidates", headers={"x-api-key": key}, params={"skill": ["python"]}
        )
        assert r.status_code == 200
        return [c["name"] for c in r.json()["items"]]

    acme = names_for(keys["acme"])
    globex = names_for(keys["globex"])

    assert any("Acme" in n for n in acme)
    assert all("Globex" not in n for n in acme)
    assert any("Globex" in n for n in globex)
    assert all("Acme" not in n for n in globex)


def test_match_results_are_owned_by_their_creator(client, keys):
    r = client.post(
        "/match",
        headers={"x-api-key": keys["acme"]},
        json={"profile": {"skills": ["python"]}, "job_description": "Python"},
    )
    rid = r.json()["id"]

    assert (
        client.get(f"/results/{rid}", headers={"x-api-key": keys["globex"]}).status_code
        == 404
    )


def test_unowned_rows_are_invisible_to_everyone(client, keys):
    """Rows the migration could not attribute must not leak to an arbitrary tenant."""
    from sqlalchemy import text
    from app.sync_db import sync_engine

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO results (id, kind, client_id, status, payload) "
                "VALUES ('legacy-orphan', 'extract', NULL, 'done', '{}')"
            )
        )

    for key in keys.values():
        got = client.get("/results/legacy-orphan", headers={"x-api-key": key})
        assert got.status_code == 404
