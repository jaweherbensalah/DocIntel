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
