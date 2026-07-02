# ---- Stage 1: builder ----
# Installs dependencies in an isolated stage so build caches and any
# compilation leftovers never make it into the final image.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy only the requirements first so this layer is cached unless deps change.
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# Copy the application source, compile it to bytecode, then delete every .py
# source file. Only compiled .pyc bytecode is carried into the runtime image,
# so the proprietary pipeline logic is not readable as plain source.
#   -b : write legacy "module.pyc" next to the source (not in __pycache__),
#        so the .pyc can be imported directly once the .py is removed.
COPY app ./app
RUN python -m compileall -b -q app \
    && find app -type f -name '*.py' -delete \
    && find app -type d -name '__pycache__' -prune -exec rm -rf {} +

# ---- Stage 2: runtime ----
# Minimal image that only carries the installed packages and the app code.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Bring in the dependencies installed in the builder stage.
COPY --from=builder /install /usr/local

# Copy only the compiled bytecode from the builder (no .py source, no tests,
# fixtures, docs, or VCS metadata).
COPY --from=builder /app/app ./app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
