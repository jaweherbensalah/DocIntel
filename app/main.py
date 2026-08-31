import hashlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_session
from app.llm import get_provider
from app.models import Client, Match, Profile, ProfileSkill, Result
from app import budget, limits
from app.observability import (
    configure_logging,
    render_metrics,
    request_context_middleware,
    request_id_var,
)
from app.persistence import build_match, matches_to_dict, profile_to_dict
from app.schemas import (
    BatchMatchRequest,
    BatchMatchResponse,
    CandidateSearchResponse,
    CandidateSummary,
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
    # Schema is owned by Alembic, not the app, so replicas cannot race it.
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


async def require_client(
    response: Response,
    x_api_key: str = Header(default=None, description="Client API key."),
    session: AsyncSession = Depends(get_session),
) -> Client:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )

    # Looking the key up by hash keeps the comparison off the response-time
    # side channel that a plain string compare would open.
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    client = await session.scalar(
        select(Client).where(Client.api_key_hash == key_hash, Client.active.is_(True))
    )
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )

    decision = limits.check(client.id, client.rate_limit_per_minute)
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
    return client


async def reserve_budget(
    session: AsyncSession,
    client: Client,
    operation: str,
    characters: int,
    units: int = 1,
) -> budget.Reservation:
    cost = budget.estimate_cents(operation, characters, units)
    try:
        return await budget.reserve(
            session, client.id, client.monthly_budget_cents, operation, cost
        )
    except budget.BudgetExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"monthly budget exhausted for {exc.period}",
        ) from exc


BUDGET_RESPONSE = {
    "model": ErrorResponse,
    "description": "The client's monthly budget is exhausted.",
}
RATE_RESPONSE = {
    "model": ErrorResponse,
    "description": "The client's rate limit was exceeded; see Retry-After.",
}


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
        402: BUDGET_RESPONSE,
        422: {
            "model": ErrorResponse,
            "description": "The uploaded file was empty or unreadable.",
        },
        429: RATE_RESPONSE,
    },
)
async def extract(
    file: UploadFile = File(..., description="CV document to process (UTF-8 text)."),
    client: Client = Depends(require_client),
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

    # Charged before the model runs, so simultaneous requests cannot each see
    # an affordable balance. The worker settles the real cost afterwards.
    reservation = await reserve_budget(session, client, "extract", len(text))

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="extract", status="pending", payload=None))
    await session.commit()

    extract_profile.delay(rid, text, request_id_var.get(), reservation.event_id)
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
        402: BUDGET_RESPONSE,
        422: {
            "model": ErrorResponse,
            "description": "The request body failed validation.",
        },
        429: RATE_RESPONSE,
    },
)
async def match(
    body: MatchRequest,
    client: Client = Depends(require_client),
    session: AsyncSession = Depends(get_session),
):
    reservation = await reserve_budget(
        session, client, "match", len(body.job_description)
    )
    result = get_provider().match(body.profile.model_dump(), body.job_description)

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="match", status="done", payload=json.dumps(result)))
    session.add(
        build_match(
            rid, body.job_description, result, candidate_name=body.profile.name
        )
    )
    await session.commit()
    await budget.settle(session, reservation.event_id, reservation.estimated_cents)
    return MatchResponse(id=rid, **result)


@app.post(
    "/batch-match",
    response_model=BatchMatchResponse,
    tags=["matching"],
    summary="Score many profiles against one job description",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        402: BUDGET_RESPONSE,
        422: {
            "model": ErrorResponse,
            "description": "The request body failed validation.",
        },
        429: RATE_RESPONSE,
    },
)
async def batch_match(
    body: BatchMatchRequest,
    client: Client = Depends(require_client),
    session: AsyncSession = Depends(get_session),
):
    # A batch of 50 costs roughly 50 times one match, so it is priced per
    # profile rather than per request.
    reservation = await reserve_budget(
        session,
        client,
        "batch_match",
        len(body.job_description),
        units=len(body.profiles),
    )
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
    for entry, (profile, result) in zip(shortlist, scored):
        session.add(
            build_match(
                rid,
                body.job_description,
                result,
                rank=entry.rank,
                candidate_name=entry.name,
            )
        )
    await session.commit()
    await budget.settle(session, reservation.event_id, reservation.estimated_cents)
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
        429: RATE_RESPONSE,
    },
)
async def get_result(
    rid: str,
    client: Client = Depends(require_client),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Result)
        .where(Result.id == rid)
        .options(
            selectinload(Result.profile).selectinload(Profile.skills),
            selectinload(Result.matches),
        )
    )
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="result not found")
    return ResultResponse(
        id=obj.id, kind=obj.kind, status=obj.status, result=_read_result(obj)
    )


def _read_result(obj: Result) -> Optional[dict]:
    """Prefer the normalised tables, fall back to the legacy JSON blob."""
    if obj.kind == "extract" and obj.profile is not None:
        return profile_to_dict(obj.profile)
    if obj.kind in ("match", "batch_match"):
        normalised = matches_to_dict(obj)
        if normalised is not None:
            return normalised
    return json.loads(obj.payload) if obj.payload else None


@app.get(
    "/candidates",
    response_model=CandidateSearchResponse,
    tags=["profiles"],
    summary="Search extracted candidates by skill and experience",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        429: RATE_RESPONSE,
    },
)
async def search_candidates(
    skill: List[str] = Query(
        default=[],
        description="Skill to require. Repeat the parameter to require several.",
        examples=[["python", "docker"]],
    ),
    min_years: int = Query(
        default=0, ge=0, description="Minimum years of experience."
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client: Client = Depends(require_client),
    session: AsyncSession = Depends(get_session),
):
    matching = select(Profile.id).where(Profile.years_experience >= min_years)

    wanted = {s.strip().lower() for s in skill if s.strip()}
    if wanted:
        matching = (
            matching.join(ProfileSkill, ProfileSkill.profile_id == Profile.id)
            .where(ProfileSkill.skill.in_(wanted))
            .group_by(Profile.id)
            # every requested skill, not just one of them
            .having(func.count(func.distinct(ProfileSkill.skill)) == len(wanted))
        )

    matching = matching.subquery()
    total = await session.scalar(select(func.count()).select_from(matching)) or 0

    page = (
        select(Profile)
        .where(Profile.id.in_(select(matching.c.id)))
        .options(selectinload(Profile.skills))
        .order_by(Profile.years_experience.desc(), Profile.id)
        .limit(limit)
        .offset(offset)
    )
    profiles = (await session.execute(page)).scalars().all()

    return CandidateSearchResponse(
        total=total,
        items=[
            CandidateSummary(
                result_id=p.result_id,
                name=p.name or "",
                years_experience=p.years_experience,
                skills=sorted(s.skill for s in p.skills),
            )
            for p in profiles
        ],
    )
