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
