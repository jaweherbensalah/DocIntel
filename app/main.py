import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import engine, get_session
from app.llm import get_provider
from app.models import Base, Result
from app.observability import (
    configure_logging,
    render_metrics,
    request_context_middleware,
    request_id_var,
)
from app.schemas import (
    BatchMatchRequest,
    BatchMatchResponse,
    ErrorResponse,
    ExtractAcceptedResponse,
    HealthResponse,
    MatchRequest,
    MatchResponse,
    ResultResponse,
    ShortlistEntry,
)
from app.tasks import extract_profile

configure_logging()
logger = logging.getLogger("docintel")

UPLOAD_DIR = "uploads"

API_DESCRIPTION = """
`docintel` extracts a structured candidate profile from a CV and scores it
against a job description.

### Typical flow
1. `POST /extract` — upload a CV. Extraction runs in the background, so you get
   back a result `id` and `status: "pending"` immediately (`202 Accepted`).
2. `GET /results/{id}` — poll until `status` becomes `done`, then read the
   extracted `profile`.
3. `POST /match` — send a profile plus a job description, receive a `score`,
   matched/missing skills and a short rationale.

### Authentication
`POST /extract` and `POST /match` require an API key in the `x-api-key`
header. Requests without a valid key receive `401 Unauthorized`.
"""

tags_metadata = [
    {"name": "system", "description": "Service health and liveness."},
    {
        "name": "profiles",
        "description": "Extract profiles from CVs and fetch stored results.",
    },
    {
        "name": "matching",
        "description": "Score candidate profiles against job descriptions.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="docintel API",
    version="1.0.0",
    summary="Extract structured profiles from CVs and score them against jobs.",
    description=API_DESCRIPTION,
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Correlation id + per-request logging and HTTP metrics.
app.middleware("http")(request_context_middleware)


@app.get(
    "/metrics",
    tags=["system"],
    summary="Prometheus metrics",
    include_in_schema=False,
)
async def metrics():
    data, content_type = render_metrics()
    return Response(content=data, media_type=content_type)


def check_auth(x_api_key: str = Header(default=None, description="Client API key.")):
    # Constant-time comparison to avoid leaking the key via response timing.
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Liveness check",
)
async def health():
    return HealthResponse(status="ok")


@app.post(
    "/extract",
    response_model=ExtractAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["profiles"],
    summary="Submit a CV for asynchronous extraction",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        422: {
            "model": ErrorResponse,
            "description": "The uploaded file was empty or unreadable.",
        },
    },
)
async def extract(
    file: UploadFile = File(..., description="CV document to process (UTF-8 text)."),
    _=Depends(check_auth),
    session: AsyncSession = Depends(get_session),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="uploaded file is empty")

    # keep a copy of the upload around so we can debug extractions
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        # Strip any directory components from the client-supplied filename so a
        # name like "../../etc/passwd" cannot escape the uploads directory.
        safe_name = os.path.basename(file.filename or "upload")
        with open(os.path.join(UPLOAD_DIR, safe_name), "wb") as f:
            f.write(content)
    except Exception as e:
        logger.warning("could not save upload: %s", e)

    text = content.decode("utf-8", errors="ignore")

    # Record the job as pending, then hand the heavy work to a background
    # worker and return immediately instead of blocking the request.
    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="extract", status="pending", payload=None))
    await session.commit()

    # Propagate the request id so the worker's logs correlate with this request.
    extract_profile.delay(rid, text, request_id_var.get())
    logger.info(
        "extraction enqueued",
        extra={"extra_fields": {"rid": rid, "bytes": len(content)}},
    )

    return ExtractAcceptedResponse(id=rid, status="pending")


@app.post(
    "/match",
    response_model=MatchResponse,
    tags=["matching"],
    summary="Score a profile against a job description",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        422: {
            "model": ErrorResponse,
            "description": "The request body failed validation.",
        },
    },
)
async def match(
    body: MatchRequest,
    _=Depends(check_auth),
    session: AsyncSession = Depends(get_session),
):
    result = get_provider().match(body.profile.model_dump(), body.job_description)

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="match", status="done", payload=json.dumps(result)))
    await session.commit()
    return MatchResponse(id=rid, **result)


@app.post(
    "/batch-match",
    response_model=BatchMatchResponse,
    tags=["matching"],
    summary="Score many profiles against one job description",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        422: {
            "model": ErrorResponse,
            "description": "The request body failed validation.",
        },
    },
)
async def batch_match(
    body: BatchMatchRequest,
    _=Depends(check_auth),
    session: AsyncSession = Depends(get_session),
):
    provider = get_provider()
    scored = [
        (profile, provider.match(profile.model_dump(), body.job_description))
        for profile in body.profiles
    ]
    # Rank best-first; Python's sort is stable, so ties keep input order.
    scored.sort(key=lambda pair: pair[1]["score"], reverse=True)

    shortlist = [
        ShortlistEntry(
            rank=i + 1,
            name=profile.name or "Unknown",
            score=result["score"],
            matched_skills=result["matched_skills"],
            missing_skills=result["missing_skills"],
            rationale=result["rationale"],
        )
        for i, (profile, result) in enumerate(scored)
    ]

    rid = uuid.uuid4().hex
    payload = {
        "job_description": body.job_description,
        "shortlist": [entry.model_dump() for entry in shortlist],
    }
    session.add(
        Result(id=rid, kind="batch_match", status="done", payload=json.dumps(payload))
    )
    await session.commit()
    return BatchMatchResponse(
        id=rid, job_description=body.job_description, shortlist=shortlist
    )


@app.get(
    "/results/{rid}",
    response_model=ResultResponse,
    tags=["profiles"],
    summary="Fetch a previously stored result",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        404: {"model": ErrorResponse, "description": "No result exists with that id."},
    },
)
async def get_result(
    rid: str,
    _=Depends(check_auth),
    session: AsyncSession = Depends(get_session),
):
    obj = await session.get(Result, rid)
    if obj is None:
        raise HTTPException(status_code=404, detail="result not found")
    result = json.loads(obj.payload) if obj.payload else None
    return ResultResponse(id=obj.id, kind=obj.kind, status=obj.status, result=result)
