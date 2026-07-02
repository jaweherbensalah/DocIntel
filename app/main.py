import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import engine, session
from app.llm import get_provider
from app.models import Base, Result
from app.schemas import (
    ErrorResponse,
    ExtractResponse,
    HealthResponse,
    MatchRequest,
    MatchResponse,
    Profile,
    ResultResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("docintel")

UPLOAD_DIR = "uploads"

API_DESCRIPTION = """
`docintel` extracts a structured candidate profile from a CV and scores it
against a job description.

### Typical flow
1. `POST /extract` — upload a CV, receive a structured `profile` and a result `id`.
2. `POST /match` — send a profile plus a job description, receive a `score`,
   matched/missing skills and a short rationale.
3. `GET /results/{id}` — fetch any previously stored extract or match result.

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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_auth(x_api_key: str = Header(default=None, description="Client API key.")):
    if x_api_key != settings.api_key:
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
    response_model=ExtractResponse,
    tags=["profiles"],
    summary="Extract a structured profile from a CV",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
        422: {
            "model": ErrorResponse,
            "description": "The uploaded file was empty or unreadable.",
        },
        502: {
            "model": ErrorResponse,
            "description": "The extraction provider failed.",
        },
    },
)
async def extract(
    file: UploadFile = File(..., description="CV document to process (UTF-8 text)."),
    _=Depends(check_auth),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="uploaded file is empty")

    # keep a copy of the upload around so we can debug extractions
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(os.path.join(UPLOAD_DIR, file.filename), "wb") as f:
            f.write(content)
    except Exception as e:
        logger.warning("could not save upload: %s", e)

    text = content.decode("utf-8", errors="ignore")

    try:
        profile = get_provider().extract(text)
    except Exception:
        logger.exception("extraction provider failed")
        raise HTTPException(status_code=502, detail="extraction provider failed")

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="extract", payload=json.dumps(profile)))
    await session.commit()
    return ExtractResponse(id=rid, profile=Profile(**profile))


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
async def match(body: MatchRequest, _=Depends(check_auth)):
    result = get_provider().match(body.profile.model_dump(), body.job_description)

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="match", payload=json.dumps(result)))
    await session.commit()
    return MatchResponse(id=rid, **result)


@app.get(
    "/results/{rid}",
    response_model=ResultResponse,
    tags=["profiles"],
    summary="Fetch a previously stored result",
    responses={
        404: {"model": ErrorResponse, "description": "No result exists with that id."},
    },
)
async def get_result(rid: str):
    obj = await session.get(Result, rid)
    if obj is None:
        raise HTTPException(status_code=404, detail="result not found")
    return ResultResponse(id=obj.id, kind=obj.kind, result=json.loads(obj.payload))
