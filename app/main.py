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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="docintel", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_auth(x_api_key: str = Header(default=None)):
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")


@app.post("/extract", response_model=ExtractResponse)
async def extract(file: UploadFile = File(...), _=Depends(check_auth)):
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


@app.post("/match", response_model=MatchResponse)
async def match(body: MatchRequest, _=Depends(check_auth)):
    result = get_provider().match(body.profile.model_dump(), body.job_description)

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="match", payload=json.dumps(result)))
    await session.commit()
    return MatchResponse(id=rid, **result)


@app.get("/results/{rid}", response_model=ResultResponse)
async def get_result(rid: str):
    obj = await session.get(Result, rid)
    if obj is None:
        raise HTTPException(status_code=404, detail="result not found")
    return ResultResponse(id=obj.id, kind=obj.kind, result=json.loads(obj.payload))
