import os

os.environ["DATABASE_URL"] = (
    "sqlite+aiosqlite:///./test_docintel.db"  # tests run on SQLite
)
os.environ["API_KEY"] = "test-key"  # required by config; set before app import

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import settings
from app.celery_app import celery_app
from app.main import app

settings.llm_latency = 0  # don't wait for the fake model in tests

# Run Celery tasks inline (no broker/worker needed) so tests stay self-contained.
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def schema():
    """Build the test schema with the real migrations, so drift is caught here."""
    db = ROOT / "test_docintel.db"
    db.unlink(missing_ok=True)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield
    db.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_extract_requires_auth(client):
    r = client.post("/extract", files={"file": ("cv.txt", b"Python developer")})
    assert r.status_code == 401


def test_extract_accepts_and_completes(client):
    r = client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Jane Doe\nPython and FastAPI, 5 years")},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending"
    rid = r.json()["id"]
    # Celery runs eagerly in tests, so the extraction has already finished.
    got = client.get(f"/results/{rid}", headers={"x-api-key": settings.api_key})
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "done"
    assert "python" in body["result"]["skills"]


def test_match_ok(client):
    r = client.post(
        "/match",
        headers={"x-api-key": settings.api_key},
        json={
            "profile": {"skills": ["python", "fastapi"]},
            "job_description": "Python FastAPI Kubernetes",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert 0 <= body["score"] <= 100
    assert "python" in body["matched_skills"]
    assert "id" in body


def test_match_requires_auth(client):
    r = client.post(
        "/match",
        json={"profile": {"skills": []}, "job_description": "x"},
    )
    assert r.status_code == 401


def test_match_validation_error(client):
    # job_description is required -> 422
    r = client.post(
        "/match",
        headers={"x-api-key": settings.api_key},
        json={"profile": {"skills": []}},
    )
    assert r.status_code == 422


def test_extract_empty_file_returns_422(client):
    r = client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"")},
    )
    assert r.status_code == 422


def test_results_requires_auth(client):
    r = client.get("/results/anything")
    assert r.status_code == 401


def test_results_not_found_returns_404(client):
    r = client.get(
        "/results/does-not-exist",
        headers={"x-api-key": settings.api_key},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "result not found"


def test_result_roundtrip(client):
    r = client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Jane Doe\nPython, 3 years")},
    )
    rid = r.json()["id"]
    got = client.get(
        f"/results/{rid}",
        headers={"x-api-key": settings.api_key},
    )
    assert got.status_code == 200
    body = got.json()
    assert body["id"] == rid
    assert body["kind"] == "extract"
    assert body["status"] == "done"
    assert "python" in body["result"]["skills"]


def test_batch_match_ranks_candidates(client):
    r = client.post(
        "/batch-match",
        headers={"x-api-key": settings.api_key},
        json={
            "profiles": [
                {"name": "A", "skills": ["python"]},
                {"name": "B", "skills": ["python", "fastapi", "kubernetes"]},
                {"name": "C", "skills": []},
            ],
            "job_description": "Python FastAPI Kubernetes",
        },
    )
    assert r.status_code == 200
    shortlist = r.json()["shortlist"]
    assert [e["name"] for e in shortlist] == ["B", "A", "C"]
    assert [e["rank"] for e in shortlist] == [1, 2, 3]
    assert shortlist[0]["score"] >= shortlist[1]["score"] >= shortlist[2]["score"]


def test_batch_match_requires_auth(client):
    r = client.post(
        "/batch-match",
        json={"profiles": [{"skills": []}], "job_description": "x"},
    )
    assert r.status_code == 401


def test_batch_match_empty_profiles_is_422(client):
    r = client.post(
        "/batch-match",
        headers={"x-api-key": settings.api_key},
        json={"profiles": [], "job_description": "x"},
    )
    assert r.status_code == 422


def test_metrics_endpoint_exposes_prometheus(client):
    # Generate at least one request so a counter sample exists.
    client.get("/health")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text


def test_response_carries_request_id(client):
    r = client.get("/health")
    assert r.headers.get("X-Request-ID")


def test_incoming_request_id_is_echoed(client):
    r = client.get("/health", headers={"x-request-id": "trace-abc-123"})
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


def test_candidates_requires_auth(client):
    assert client.get("/candidates").status_code == 401


def test_candidates_search_by_skill_and_experience(client):
    client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Ana Ruiz\nPython, Docker and FastAPI, 7 years")},
    )
    client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Bo Lee\nJava only, 2 years")},
    )

    r = client.get(
        "/candidates",
        headers={"x-api-key": settings.api_key},
        params={"skill": ["python", "docker"], "min_years": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    names = [c["name"] for c in body["items"]]
    assert any("Ana Ruiz" in n for n in names)
    assert all("Bo Lee" not in n for n in names)


def test_candidates_requires_every_requested_skill(client):
    # "java" alone matches Bo Lee, but not combined with "python".
    r = client.get(
        "/candidates",
        headers={"x-api-key": settings.api_key},
        params={"skill": ["java", "python"]},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_extract_result_is_served_from_normalised_tables(client):
    r = client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Cy Wong\nPython and Kubernetes, 4 years")},
    )
    rid = r.json()["id"]
    got = client.get(f"/results/{rid}", headers={"x-api-key": settings.api_key}).json()
    assert got["result"]["years_experience"] == 4
    assert "kubernetes" in got["result"]["skills"]
