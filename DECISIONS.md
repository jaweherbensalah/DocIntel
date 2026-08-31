# Engineering decisions & ticket log

This document records each ticket we took on: what was wrong, what we changed,
why we chose that approach, the trade-offs we accepted, and how to verify it.

---

## Ticket 4 — The image is too big

### What was wrong

The original [`Dockerfile`](Dockerfile) was a single-stage build:

```dockerfile
FROM python:3.11
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Three problems made the shipped image large and slow to build/push/pull:

1. **Full base image.** `python:3.11` (not `-slim`) bundles a complete build
   toolchain (gcc, dev headers, etc.) that is only needed to *compile* packages,
   not to *run* the app. The base alone is ~1.0 GB.
2. **No `.dockerignore`.** `COPY . .` copied the entire working directory into
   the build context — including `.git/`, `uploads/`, local virtualenvs, caches
   and test databases. The measured build context was ~100 MB.
3. **Single stage.** Build-time artifacts (pip's download cache, compilation
   leftovers) stayed in the final image.

### What we changed

- **Added a [`.dockerignore`](.dockerignore)** so only what the image needs
  enters the build context. Context dropped from ~100 MB to **283 B**.
- **Switched the base to `python:3.11-slim`** for both stages.
- **Multi-stage build:**
  - `builder` stage installs dependencies into an isolated prefix
    (`pip install --prefix=/install`).
  - `runtime` stage copies only the installed packages (`/install`) and the
    application code (`COPY app ./app`). No pip cache, no compilers, no source
    tree beyond the app package.
- **Requirements copied before the app** so the dependency layer is cached and
  only re-installed when `requirements.txt` changes.

### Why this approach (trade-offs)

- **`slim` instead of `alpine`.** Alpine is smaller still, but it uses `musl`
  libc, which breaks or forces slow source-compiles of common pre-built wheels
  (`asyncpg`, `pydantic-core`). `slim` keeps fast, reliable wheels while
  cutting most of the fat. We judged build reliability + speed worth the extra
  few MB over Alpine.
- **Multi-stage over `pip --no-cache-dir` alone.** Even with cache disabled, a
  single stage keeps build tooling around. Multi-stage guarantees the runtime
  image carries only what it runs.

### Result (before → after)

| | Base image | Build context | Final image |
|---|---|---|---|
| Before | `python:3.11` (~1.0 GB base) | ~100 MB | > 1 GB |
| After | `python:3.11-slim` | 283 B | **327 MB** |

> The "before" final image was not built to completion because the full base
> layer pulled too slowly on this connection — which is itself the business
> case for the ticket. The base-layer size (~1.0 GB, published on Docker Hub)
> already exceeds the entire optimized image.

### How to verify

```bash
docker build -t docintel:after .
docker images docintel:after --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
# -> docintel:after  327MB
```

---

## Ticket 3 — The image leaks our code

### What was wrong

The original image copied the raw project into the container (`COPY . .`) and
ran as root. Anyone who could `docker exec` (or `docker cp`) into a running
container could read every `.py` file — our proprietary extraction/matching
pipeline — straight off the filesystem in plain text.

### What we changed

In the builder stage we now **compile the app to bytecode and delete the
source** before it ever reaches the runtime image:

```dockerfile
COPY app ./app
RUN python -m compileall -b -q app \
    && find app -type f -name '*.py' -delete \
    && find app -type d -name '__pycache__' -prune -exec rm -rf {} +
```

The runtime stage then copies **only the compiled package** from the builder:

```dockerfile
COPY --from=builder /app/app ./app
```

- `compileall -b` writes legacy `module.pyc` files *next to* each source file
  (instead of inside `__pycache__/`). Once the `.py` is removed, Python imports
  the `.pyc` directly ("sourceless import").
- The final image contains `main.pyc`, `llm.pyc`, `config.pyc`, … and **no
  `.py` files at all**.
- Combined with the already-present non-root `appuser`, an attacker who gets a
  shell in the container finds no readable source and no root privileges.

### Why this approach (trade-offs)

This is **defense-in-depth, not encryption.** We are explicit about that:

- Python is an interpreted language. Bytecode is *not* secret — tools like
  `decompyle3` / `uncompyle6` can reconstruct approximate source from `.pyc`.
  There is **no way to make a Python image truly unreadable** to someone who
  controls the container; anything the interpreter can run, a determined
  attacker can recover.
- What we *can* do — and did — is remove the easy path: no plaintext source
  lying on disk, no root shell, minimal tooling in the image. This raises the
  cost of extraction from "cat a file" to "decompile bytecode," which is a
  meaningful, honest improvement.

Stronger options we considered and deliberately did **not** take (avoiding
gold-plating for this stage):

- **Cython → native `.so`.** Compiling modules to C extensions is much harder to
  reverse than `.pyc`, but it adds a compiler toolchain to the builder, slows
  builds, and complicates debugging/stack traces. Worth it only if the business
  truly requires it.
- **Commercial obfuscators (e.g. PyArmor).** Extra dependency and licensing for
  a still-not-unbreakable result.
- **Serving the sensitive logic from a separate backend the client never
  runs.** The architecturally "correct" answer if the IP is critical, but out
  of scope for a container-hardening ticket.

### Result

| | Source in image | Runs as | Import works |
|---|---|---|---|
| Before | all `.py` readable | root | yes |
| After | `.pyc` bytecode only, **no `.py`** | non-root `appuser` | yes |

### How to verify

```bash
docker build -t docintel:secure .

# No .py source in the image:
docker run --rm docintel:secure sh -c "find /app -name '*.py'"      # -> (empty)

# Only compiled bytecode is present:
docker run --rm docintel:secure sh -c "find /app -name '*.pyc'"     # -> app/*.pyc

# App still loads from bytecode:
docker run --rm docintel:secure python -c "import app.main; print('import OK')"
```

---

## Ticket 5 — The API contract is loose

### What was wrong

- **Errors returned `200 OK`.** `/extract` returned `{"error": ...}` and
  `/results/{id}` returned `{"error": "not found"}` — both with a `200` status,
  so a client could not detect failure from the status code.
- **Untyped request body.** `/match` accepted `body: dict` and reached into it
  with `.get(...)`, so malformed requests silently produced empty results
  instead of a validation error.
- **Unused schemas.** `app/schemas.py` defined `Profile` / `MatchRequest` /
  `MatchResult` that no endpoint actually used.
- **Inconsistent shapes.** Success and error responses had no common structure.

### What we changed

- **Typed request/response models** for every endpoint
  (`ExtractResponse`, `MatchRequest`, `MatchResponse`, `ResultResponse`,
  `HealthResponse`) wired up via FastAPI `response_model`.
- **Correct status codes:**
  - `401` — missing/invalid API key (unchanged, now also declared).
  - `404` — `GET /results/{id}` for an unknown id (was `200`).
  - `422` — empty upload to `/extract`, or a `/match` body that fails
    validation.
  - `502` — the extraction provider raised (was a `200 {"error": ...}`).
- **One consistent error envelope** — `{"detail": "..."}` (`ErrorResponse`),
  matching FastAPI's native `HTTPException` shape.
- **`Profile` is lenient** (fields default to empty) so an integrator can send a
  partial profile to `/match` without reconstructing a full extract result.

### Why this approach (trade-offs)

- **`502` (not `500`) for provider failure** — the model call is an upstream
  dependency; `502 Bad Gateway` tells the integrator it is an upstream problem,
  not a bug in their request.
- **Scope kept to the contract.** `GET /results/{id}` deliberately still has no
  auth here — adding authentication belongs to **Ticket 2**, so we keep this
  commit focused and avoid documenting auth we have not yet enforced.

### How to verify

```bash
pytest -q
# covers: 404 on missing result, 422 on empty upload / invalid match body,
# 401 on unauthenticated calls, and a full extract -> results round-trip.
```

---

## Ticket 6 — The API docs are poor

### What was wrong

The OpenAPI schema was the bare FastAPI default: title `"docintel"`, no
description, no examples, no documented error responses, and endpoints returned
undeclared ad-hoc dicts — so the Swagger UI did not reflect real behaviour and
could not be integrated against on its own.

### What we changed

- **App-level metadata:** meaningful `title`, `version`, `summary` and a
  Markdown `description` documenting the typical flow and the auth requirement.
- **Tags** (`system`, `profiles`, `matching`) grouping the endpoints.
- **Per-endpoint `summary`** and **declared `response_model`** so every success
  shape is documented.
- **Documented error responses** (`401`, `404`, `422`, `502`) each pointing at
  `ErrorResponse`, so the failure modes appear in the schema.
- **Field-level `description` and `examples`** on every schema field, so request
  and response bodies render with realistic sample values in Swagger UI.

### Result

The generated docs now match real behaviour and are self-sufficient.

### How to verify

```bash
# with the app running (uvicorn app.main:app):
#   Swagger UI:   http://localhost:8000/docs
#   Raw schema:   http://localhost:8000/openapi.json
```

---

## Ticket 2 — The authentication is unsafe

### What was wrong

Treating the service as internet-facing and handling personal data (CVs), the
starter had five concrete flaws:

1. **Hardcoded secret.** `api_key` defaulted to `"dev-secret-key-change-me"` in
   source — a working password baked into the code.
2. **Broken access control.** `GET /results/{id}` had no auth at all, so anyone
   could read any stored CV/profile by id.
3. **Timing-unsafe comparison.** `x_api_key != settings.api_key` short-circuits,
   leaking the key one character at a time via response timing.
4. **Unsafe CORS.** `allow_origins=["*"]` together with
   `allow_credentials=True` — an invalid, unsafe combination.
5. **Path traversal.** Uploads were saved under the raw client filename, so
   `../../…` could escape the uploads directory.

### What we changed

- **Secret from the environment, fail closed.** `api_key` has **no default**;
  the app refuses to start unless `API_KEY` is supplied. A dev fallback lives
  only in `docker-compose.yml` (`${API_KEY:-…}`), never in the app source.
- **Auth on `/results`.** Added `Depends(check_auth)` and documented its `401`.
- **Constant-time check.** `secrets.compare_digest(...)` (and reject a missing
  key) so comparison time no longer depends on the key.
- **Safe CORS.** `allow_credentials=False` (we authenticate with a header, not
  cookies) and `cors_origins` defaults to empty.
- **Filename sanitised.** `os.path.basename(...)` strips directory components
  before writing an upload.
- **`debug` off by default** so SQL echo cannot leak document contents to logs.

### Why this approach (trade-offs)

- **Fail closed vs. convenience.** Requiring the key means a bare `python`
  process with no env will not boot. That is intentional for a service handling
  personal data; `docker compose up` still works because compose provides a dev
  key, and `.env.example` documents it.
- **Scope.** This is per-key auth for a single trusted client. Per-client
  credentials and tenant isolation are **Ticket 14**; rate/budget limits are
  **Ticket 12** — deliberately out of scope here.

### How to verify

```bash
pytest -q
# includes test_results_requires_auth (401 without a key) plus the existing
# extract/match auth checks.
```

---

## Ticket 1 — The pipeline doesn't scale

### What was wrong

`POST /extract` ran the model **inline**, inside the request: read file → run
extraction (a blocking `sleep`/model call) → save → respond. A single slow
document tied up a web worker for the whole duration, so the service buckled
under load. (The fake provider even used a blocking `time.sleep`, freezing the
whole async event loop.)

### What we changed

We moved the heavy work onto a **task queue**:

- **`POST /extract`** now records a `pending` result, enqueues a Celery job, and
  returns **`202 Accepted`** with the `id` immediately — it no longer runs the
  model on the request path.
- A **Celery worker** (`app/tasks.py`, `app/celery_app.py`) consumes the job,
  runs the extraction, and updates the row to `done` (or `failed`).
- **`GET /results/{id}`** now reports `status` (`pending`/`done`/`failed`) plus
  the `result` once ready — the client polls it.
- **Redis** is the broker (and result backend); a `worker` service was added to
  `docker-compose.yml` alongside `redis`.

Supporting changes:

- **Per-request DB sessions** (`get_session` in `app/db.py`). The starter shared
  one global session for the whole app, which returns cached/stale rows — so a
  later read would never see `pending` flip to `done`. A fresh session per
  request fixes that and is safe under concurrency.
- **`Result.status`** column added; `payload` is now nullable (null until done).
- **Sync engine for the worker** (`app/sync_db.py`): Celery tasks are
  synchronous, so they use a psycopg2 engine derived from the same
  `DATABASE_URL`, rather than the API's async engine.

### How we manage the queue (this is the part that matters)

- **`task_acks_late=True`** — a job is acknowledged only *after* it finishes, so
  if a worker crashes mid-job the job is redelivered instead of lost.
- **`worker_prefetch_multiplier=1`** — each worker slot holds one job at a time,
  so a slow job can't block a batch it greedily prefetched.
- **Retries** — the task retries (bounded) on failure and marks the row
  `failed` when exhausted.

### Why this approach (trade-offs)

- **Only `/extract` is queued.** It is the heavy, upload-triggered path the
  ticket calls out. `/match` operates on an already-small profile and is left
  synchronous; it could adopt the identical pattern if needed. (Avoiding
  gold-plating.)
- **Polling, not push.** `GET /results/{id}` polling is the simplest correct
  contract; SSE/webhooks are noted as a later enhancement.
- **Schema change needs a volume reset.** Because there are no migrations yet
  (that is Ticket 11), an existing Postgres volume must be reset once:
  `docker compose down -v`.

### How to verify

```bash
# Unit/integration (Celery runs eagerly in-process, no broker needed):
pytest -q          # extract returns 202 pending, then results shows done

# End to end:
docker compose down -v && docker compose up --build
# POST /extract -> 202 {id, status: pending}
# GET  /results/{id} -> {status: done, result: {...}} once the worker finishes
```

---

## Ticket 8 — New feature: batch matching

### What we added

`POST /batch-match` scores one job description against **many** candidate
profiles and returns a **ranked shortlist** (best match first). It reuses the
existing per-profile match logic, sorts by score (Python's stable sort keeps
input order on ties), and stores the result like any other
(`kind="batch_match"`), retrievable via `GET /results/{id}`.

### Why this shape

- **Reuses the match provider** instead of duplicating scoring logic.
- **Typed and documented** (`BatchMatchRequest`, `BatchMatchResponse`,
  `ShortlistEntry`) so it renders fully in the OpenAPI docs — consistent with
  Tickets 5/6.
- **Requires ≥1 profile** (422 on empty) and an API key, consistent with the
  rest of the API.
- **Kept synchronous.** Scoring is cheap string work; if profiles were scored by
  a real LLM, this would move onto the Celery queue exactly like `/extract`.

### How to verify

```bash
pytest -q   # test_batch_match_ranks_candidates asserts best-first ordering
```

---

## Ticket 10 — Ship it with CI/CD

### What we added

A GitHub Actions pipeline (`.github/workflows/ci.yml`) with three stages that
gate each other:

1. **test** — installs deps and runs the suite on every push and PR. The suite
   is fully offline (SQLite + Celery eager + fake LLM), so no services or
   secrets are needed.
2. **build-smoke** — builds the image, then brings up the full stack with
   `docker compose` and proves it works: waits for `/health`, then runs a real
   `POST /extract` → poll `GET /results/{id}` round trip through Redis and the
   worker. This is exactly the ground-rule acceptance ("`docker compose up`
   must work") enforced in CI.
3. **publish** — pushes the image to GHCR, tagged by branch, semver and commit
   SHA. Runs **only** on pushes to `main` or `v*` tags, never on PRs.

### Why this shape

- **Stages gate each other** (`needs:`), so we never publish an image that
  failed tests or couldn't boot.
- **The smoke test exercises the real async path** (queue + worker), not just a
  unit import — it's the highest-signal check that a release is shippable.
- **Publish is guarded** by `if: github.event_name == 'push'` and least-
  privilege `packages: write` permissions, so fork PRs can't push images.
- **`concurrency` cancels superseded runs** to save CI minutes.
- **Kept lean** — no multi-version test matrix and no image vulnerability
  scanning/provenance here; that supply-chain hardening is Ticket 18 and would
  be duplicated effort if added now.

### How to verify

Push to a branch and open a PR: the `test` and `build-smoke` jobs run. Merge to
`main`: `publish` additionally pushes `ghcr.io/<owner>/docintel`. Locally you can
reproduce the smoke test with:

```bash
API_KEY=ci-smoke-key docker compose up -d --build
curl -fs localhost:8000/health
```
----

## Ticket 7 — Make it observable

### What we added

Three pillars, all in `app/observability.py`:

1. **Structured logging** — every log line is JSON (`ts`, `level`, `logger`,
   `msg`, `request_id`, plus any structured `extra_fields`). Machine-parseable
   for any log stack (Loki, ELK, CloudWatch) with no regex.
2. **Metrics** — Prometheus counters/histograms for HTTP requests
   (`http_requests_total`, `http_request_duration_seconds`) and Celery tasks
   (`celery_tasks_total`, `celery_task_duration_seconds`). Scraped from
   `GET /metrics` on the API and a metrics HTTP server on the worker (port 9100).
3. **Correlation** — an `X-Request-ID` is generated (or accepted from the
   client) at the edge, bound to a `ContextVar`, echoed back in the response,
   and **propagated into the Celery task** so worker logs carry the same id.
   One request can be followed API → queue → worker in the logs.

### Why this shape

- **Request id as a `ContextVar`** means the id is available to every log call
  without threading it through function signatures, and it survives across the
  async request and into the sync task (passed explicitly as a task argument).
- **Route template as the metric label** (`/results/{rid}`, not the raw id)
  keeps Prometheus label cardinality bounded.
- **Worker metrics use `prometheus_client` multiprocess mode**
  (`PROMETHEUS_MULTIPROC_DIR`) so counters are aggregated correctly across the
  worker's prefork child processes rather than showing only one child's view.
- **`/metrics` is unauthenticated** — that's the Prometheus convention; it
  exposes no sensitive data and would normally be reachable only on the
  internal network / scrape target, not the public ingress.
- **Full distributed tracing (OpenTelemetry spans) is deliberately deferred to
  Ticket 16.** This ticket delivers the logs + metrics you operate on daily;
  request-id correlation is the pragmatic bridge until then.

### How to verify

```bash
pytest -q   # asserts /metrics exposes Prometheus text and X-Request-ID is set/echoed
```

Live, against `docker compose up`:

```bash
curl -s localhost:8000/metrics | grep http_requests_total   # API metrics
curl -s localhost:9100/metrics | grep celery_tasks_total     # worker metrics
# JSON logs with a shared request_id across API and worker:
docker compose logs api worker | grep <request-id-from-X-Request-ID-header>
```

## Ticket 9 — Deploy it to Kubernetes

### What we added

Plain manifests under `k8s/`, applied in filename order: namespace, config +
secret, Postgres (StatefulSet + PVC), Redis, the API Deployment + Service, the
Celery worker Deployment, and NetworkPolicies. `k8s/README.md` documents the
kind/minikube walkthrough.

### Why this shape

- **Plain YAML, not Helm.** There is one environment and one chart's worth of
  config here; a chart would add indirection without removing duplication.
- **Postgres is a StatefulSet with a PVC, Redis is a Deployment.** The database
  is durable state; the queue is rebuildable. Treating them the same would be
  wrong in both directions.
- **Security defaults on every pod**: non-root, `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false`, all capabilities dropped, with explicit
  `emptyDir` mounts for the few writable paths the processes actually need.
  This matters because the image runs client-supplied documents.
- **NetworkPolicies limit Postgres and Redis to the API and worker pods.** The
  data is personal (CVs); "anything in the cluster can reach the database" is
  not an acceptable default. Noted in the README that kind's default CNI does
  not enforce these.
- **Probes match how each process actually fails.** The API gets a
  `startupProbe` so slow first-boot table creation isn't mistaken for a hang;
  the worker serves no HTTP, so its liveness is a Celery `inspect ping`.
- **`terminationGracePeriodSeconds: 60` on the worker** pairs with the existing
  `task_acks_late`, so a rolling deploy finishes in-flight work instead of
  relying on redelivery.

Deliberately out of scope, and called out in `k8s/README.md` rather than left
silent: schema migrations as a pre-deploy `Job` (Ticket 11), queue-depth
autoscaling (Ticket 15), and an Ingress with TLS (environment-specific).

### How to verify

No cluster is required to evaluate them:

```bash
kubectl apply --dry-run=client -f k8s/
kubeconform -strict -summary k8s/
```

On a local cluster, see `k8s/README.md` for the full kind walkthrough
(`kind load docker-image` side-loads the image, so no registry is needed).

## Ticket 11 — Evolve the data model with zero downtime

### What was wrong

Every result was `json.dumps`-ed into a single `TEXT` column. Nothing about it
was queryable: "which candidates know Python and have 5+ years?" required
loading every row and parsing it in Python. No index could help, and the column
had no schema, so nothing validated what went into it.

### The new model

`results` stays as the job record (id, kind, status, timestamps). Around it:

| Table | Holds | Why a table |
|---|---|---|
| `profiles` | one row per extract: name, years of experience | the row we filter and sort on |
| `profile_skills` | `(profile_id, skill)` | the actual query axis, indexed |
| `matches` | one row per scored candidate | a batch of 50 becomes 50 queryable rows |

The rule applied: **normalise what you query, keep the long tail as JSON.**
`matched_skills` / `missing_skills` stay JSON columns on `matches` (JSONB on
Postgres) because nothing filters on them; skills on a profile became a real
table because that is exactly what we filter on.

`GET /candidates?skill=python&skill=docker&min_years=5` exists to make the
payoff concrete: it is one indexed query now and was impossible before.

### The migration: expand, backfill, contract

The point of the ticket is that this has to happen under live traffic, so it is
split across releases. No single step both writes and reads the new shape.

| Step | Ships in | Safe because |
|---|---|---|
| **1. Expand** (`0002_expand`) | release N | purely additive: new tables, nothing altered or dropped, so release N-1 keeps working untouched |
| **2. Dual-write** (app) | release N | writes both shapes; reads still tolerate either |
| **3. Backfill** (`0003_backfill`) | release N | batched and committed per batch, so no long transaction or lock |
| **4. Read from new** (app) | release N | falls back to `payload` for any row the backfill has not reached |
| **5. Contract** (drop `payload`) | **release N+1** | only once steps 1-4 have been live and verified |

Details that make each step actually safe:

- **The one index on the live table is `CREATE INDEX CONCURRENTLY`**, inside an
  `autocommit_block` because it cannot run in a transaction. A plain
  `CREATE INDEX` takes a write lock on `results` for its duration. Indexes on
  the new tables are ordinary, since those tables are empty at creation.
- **The backfill uses keyset pagination** on the primary key, so it always makes
  forward progress and never scans the whole table in one statement. Crucially,
  the cursor advances even when every row in a batch is skipped, so a bad row
  cannot spin the loop forever.
- **It is idempotent.** Rows that already have normalised data are skipped via
  `NOT EXISTS`, so it can be re-run after an interruption, or re-run once
  dual-writing is live to sweep up anything written in between.
- **An unparseable payload is logged and skipped, not fatal.** One bad legacy
  row should not roll back a deployment.
- **The app owns no schema any more.** `create_all` on startup was removed (two
  API replicas would race it); a `migrate` service in compose and a `Job` in
  `k8s/35-migrate-job.yaml` run `alembic upgrade head` exactly once.

### Why the contract step is deliberately not in this commit

Dropping `payload` in the same release that starts reading the new tables would
defeat the purpose: if the new read path had to be rolled back, the data it
replaced would already be gone. It ships one release later, once the dual-write
release has been verified in production:

```python
def upgrade() -> None:
    op.drop_column("results", "payload")
```

That is a metadata-only operation in Postgres, so it is instant and lock-cheap.
Leaving it out is the point, not an omission.

### Trade-offs accepted

- **`job_description` is denormalised onto every `matches` row.** A 50-candidate
  batch stores it 50 times. A `match_batches` parent table would remove the
  duplication; it is not worth the join until the text gets large.
- **Legacy `match` rows backfill with an empty `job_description`**, because the
  old payload never stored it. Nothing is lost that was ever recorded.
- **The backfill runs inside the migration.** Fine at this size. Past a few
  million rows it should move to a standalone job so a slow backfill cannot hold
  up a deploy; the batching is already written to support that.

### How to verify

```bash
pytest -q
```

`tests/test_migrations.py` is the real proof: it builds a database in the *old*
shape, seeds legacy rows of every kind plus a deliberately corrupt one, runs the
migrations against it, then asserts the normalised data is correct, that
`results.payload` was left intact (no data loss), that the bad row was skipped
rather than fatal, and that re-running the backfill produces no duplicates.
