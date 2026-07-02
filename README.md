# docintel

Small service that pulls a structured profile out of a CV and scores it against
a job description.

## Run it

The easy way — brings up the API and its Postgres database:

```bash
docker compose up --build
```

The API is then on http://localhost:8000.

To run it without Docker you need a Postgres reachable at `DATABASE_URL`
(see `docker-compose.yml`), then:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

It uses a fake LLM by default so it works offline. To use a real model, set
`LLM_PROVIDER=openai` and `OPENAI_API_KEY` in `.env`.

## Endpoints

- `POST /extract` — upload a CV file, get back a profile
- `POST /match` — send a profile + job description, get a score
- `GET /results/{id}` — fetch a previous result
- `GET /health`

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
```

## Tests

```bash
pytest
```

## Notes

- Data is stored in Postgres (see `docker-compose.yml`).
- Uploaded files are kept under `uploads/` for debugging.
