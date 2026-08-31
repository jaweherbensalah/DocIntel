"""Migration safety: seed a database in the old shape, migrate it, check it."""

import json
import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import settings

ROOT = Path(__file__).resolve().parents[1]

LEGACY_SCHEMA = """
CREATE TABLE results (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    kind VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'done',
    payload TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

LEGACY_ROWS = [
    (
        "r_extract",
        "extract",
        {"name": "Jane Doe", "skills": ["python", "fastapi"], "years_experience": 5},
    ),
    (
        "r_match",
        "match",
        {
            "score": 67,
            "matched_skills": ["python"],
            "missing_skills": ["kubernetes"],
            "rationale": "Candidate matches 1/2 required skills.",
        },
    ),
    (
        "r_batch",
        "batch_match",
        {
            "job_description": "Python Kubernetes",
            "shortlist": [
                {
                    "rank": 1,
                    "name": "Jane Doe",
                    "score": 100,
                    "matched_skills": ["python", "kubernetes"],
                    "missing_skills": [],
                    "rationale": "2/2",
                },
                {
                    "rank": 2,
                    "name": "Bo Lee",
                    "score": 50,
                    "matched_skills": ["python"],
                    "missing_skills": ["kubernetes"],
                    "rationale": "1/2",
                },
            ],
        },
    ),
]


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """A database in the pre-migration shape, with rows already in it."""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setattr(settings, "database_url", url)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(LEGACY_SCHEMA))
        for rid, kind, payload in LEGACY_ROWS:
            conn.execute(
                text(
                    "INSERT INTO results (id, kind, status, payload) "
                    "VALUES (:id, :kind, 'done', :payload)"
                ),
                {"id": rid, "kind": kind, "payload": json.dumps(payload)},
            )
        # a row the backfill cannot parse
        conn.execute(
            text(
                "INSERT INTO results (id, kind, status, payload) "
                "VALUES ('r_broken', 'extract', 'done', 'not json')"
            )
        )
    return engine


def _alembic_config():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def test_migration_backfills_and_preserves_legacy_data(legacy_db):
    command.upgrade(_alembic_config(), "head")

    with legacy_db.connect() as conn:
        profile = conn.execute(
            text("SELECT id, name, years_experience FROM profiles WHERE result_id='r_extract'")
        ).one()
        assert profile.name == "Jane Doe"
        assert profile.years_experience == 5

        skills = conn.execute(
            text("SELECT skill FROM profile_skills WHERE profile_id=:pid ORDER BY skill"),
            {"pid": profile.id},
        ).scalars().all()
        assert skills == ["fastapi", "python"]

        single = conn.execute(
            text("SELECT score, rationale, rank FROM matches WHERE result_id='r_match'")
        ).one()
        assert single.score == 67
        assert single.rank is None

        batch = conn.execute(
            text(
                "SELECT rank, candidate_name, score, job_description "
                "FROM matches WHERE result_id='r_batch' ORDER BY rank"
            )
        ).all()
        assert [r.rank for r in batch] == [1, 2]
        assert [r.candidate_name for r in batch] == ["Jane Doe", "Bo Lee"]
        assert batch[0].job_description == "Python Kubernetes"

        # No data loss: the original column is untouched.
        remaining = conn.execute(
            text("SELECT COUNT(*) FROM results WHERE payload IS NOT NULL")
        ).scalar_one()
        assert remaining == len(LEGACY_ROWS) + 1


def test_unparseable_payload_is_skipped_not_fatal(legacy_db):
    command.upgrade(_alembic_config(), "head")

    with legacy_db.connect() as conn:
        # the bad row survives and produced no normalised rows
        assert conn.execute(
            text("SELECT payload FROM results WHERE id='r_broken'")
        ).scalar_one() == "not json"
        assert conn.execute(
            text("SELECT COUNT(*) FROM profiles WHERE result_id='r_broken'")
        ).scalar_one() == 0


def test_backfill_is_idempotent(legacy_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    def counts():
        with legacy_db.connect() as conn:
            return (
                conn.execute(text("SELECT COUNT(*) FROM profiles")).scalar_one(),
                conn.execute(text("SELECT COUNT(*) FROM profile_skills")).scalar_one(),
                conn.execute(text("SELECT COUNT(*) FROM matches")).scalar_one(),
            )

    before = counts()

    # re-run the backfill as an interrupted deployment would
    command.downgrade(cfg, "0002_expand")
    command.upgrade(cfg, "head")

    assert counts() == before
