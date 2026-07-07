# docintel

Small service that pulls a structured profile out of a CV and scores it against
a job description.

## Run it

The easy way — brings up the API and its Postgres database:

```bash
docker compose up --build
```

The API is then on http://localhost:8000. Requests need an `x-api-key` header;
compose sets a development key (`dev-local-key-change-me`) by default.

To run it without Docker you need a Postgres reachable at `DATABASE_URL`
(see `docker-compose.yml`), then:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

It uses a fake LLM by default so it works offline. To use a real model, set
`LLM_PROVIDER=openai` and `OPENAI_API_KEY` in `.env`.

## Endpoints

- `POST /extract` — upload a CV file; returns `202` with a job `id` and
  `status: pending`. Extraction runs in the background on a Celery worker.
- `GET /results/{id}` — poll until `status` is `done`, then read the `profile`.
- `POST /match` — send a profile + job description, get a score.
- `POST /batch-match` — score many profiles against one job description; returns
  a ranked shortlist (best first).
- `GET /health`

All endpoints except `/health` require an `x-api-key` header.

## Examples

```bash
# extract
curl -s -X POST http://localhost:8000/extract \
  -H "x-api-key: $API_KEY" \
  -F "file=@fixtures/cv_jane_doe.txt"

# match
curl -s -X POST http://localhost:8000/match \
  -H "x-api-key: $API_KEY" \
  -H "content-type: application/json" \
  -d '{"profile": {"skills": ["python", "fastapi"]}, "job_description": "Python FastAPI Kubernetes"}'

# poll for an async extract result
curl -s http://localhost:8000/results/<id> -H "x-api-key: $API_KEY"
```

## Tests

```bash
pytest
```

## Notes

- Extraction runs asynchronously on a Celery worker (Redis broker), so the API
  stays responsive; `POST /extract` returns immediately and you poll `/results`.
- Data is stored in Postgres, each result with a `status`
  (`pending`/`done`/`failed`).
- If you ran an older version, reset the DB volume after the schema change:
  `docker compose down -v`.
- Uploaded files are kept under `uploads/` for debugging.
