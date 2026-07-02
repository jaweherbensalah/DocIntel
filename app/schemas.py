from typing import List

from pydantic import BaseModel, Field


class Profile(BaseModel):
    name: str = ""
    skills: List[str] = Field(default_factory=list)
    years_experience: int = Field(default=0, ge=0)


class ExtractResponse(BaseModel):
    id: str
    profile: Profile


class MatchRequest(BaseModel):
    profile: Profile
    job_description: str = Field(..., min_length=1)


class MatchResponse(BaseModel):
    id: str
    score: int = Field(..., ge=0, le=100)
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    rationale: str


class ResultResponse(BaseModel):
    id: str
    kind: str
    result: dict


class HealthResponse(BaseModel):
    status: str = "ok"


class ErrorResponse(BaseModel):
    detail: str
