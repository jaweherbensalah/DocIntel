import os

os.environ["DATABASE_URL"] = (
    "sqlite+aiosqlite:///./test_docintel.db"  # tests run on SQLite
)

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
