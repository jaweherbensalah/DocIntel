import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import engine, session
from app.llm import get_provider
from app.models import Base, Result

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("docintel")

UPLOAD_DIR = "uploads"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="docintel", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_auth(x_api_key: str = Header(default=None)):
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...), _=Depends(check_auth)):
    content = await file.read()

    # keep a copy of the upload around so we can debug extractions
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(os.path.join(UPLOAD_DIR, file.filename), "wb") as f:
            f.write(content)
    except Exception as e:
        logger.warning("could not save upload: %s", e)

    text = content.decode("utf-8", errors="ignore")
    logger.info("extracting profile from CV: %s", text)

    try:
        profile = get_provider().extract(text)
    except Exception as e:
        return {"error": str(e)}

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="extract", payload=json.dumps(profile)))
    await session.commit()
    return {"id": rid, "profile": profile}


@app.post("/match")
async def match(body: dict, _=Depends(check_auth)):
    profile = body.get("profile") or {}
    job_description = body.get("job_description") or ""

    result = get_provider().match(profile, job_description)

    rid = uuid.uuid4().hex
    session.add(Result(id=rid, kind="match", payload=json.dumps(result)))
    await session.commit()
    return {"id": rid, **result}


@app.get("/results/{rid}")
async def get_result(rid: str):
    obj = await session.get(Result, rid)
    if obj is None:
        return {"error": "not found"}
    return {"id": obj.id, "kind": obj.kind, "result": json.loads(obj.payload)}
