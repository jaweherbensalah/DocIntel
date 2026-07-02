import os

os.environ["DATABASE_URL"] = (
    "sqlite+aiosqlite:///./test_docintel.db"  # tests run on SQLite
)
os.environ["API_KEY"] = "test-key"  # required by config; set before app import

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

settings.llm_latency = 0  # don't wait for the fake model in tests


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_extract_requires_auth(client):
    r = client.post("/extract", files={"file": ("cv.txt", b"Python developer")})
    assert r.status_code == 401


def test_extract_ok(client):
    r = client.post(
        "/extract",
        headers={"x-api-key": settings.api_key},
        files={"file": ("cv.txt", b"Jane Doe\nPython and FastAPI, 5 years")},
    )
    assert r.status_code == 200
    assert "python" in r.json()["profile"]["skills"]


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
    assert "python" in body["result"]["skills"]
