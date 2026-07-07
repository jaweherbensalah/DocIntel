from typing import List, Optional

from pydantic import BaseModel, Field


class Profile(BaseModel):
    """A structured candidate profile extracted from a CV."""

    name: str = Field(
        default="",
        description="Candidate name (best-effort from the CV).",
        examples=["Jane Doe"],
    )
    skills: List[str] = Field(
        default_factory=list,
        description="Normalised skills detected in the document.",
        examples=[["python", "fastapi", "docker"]],
    )
    years_experience: int = Field(
        default=0,
        ge=0,
        description="Total years of experience detected.",
        examples=[5],
    )


class ExtractResponse(BaseModel):
    """Response body returned by ``POST /extract``."""

    id: str = Field(
        ...,
        description="Result id; pass to GET /results/{id} to retrieve it later.",
        examples=["a1b2c3d4e5f6"],
    )
    profile: Profile


class ExtractAcceptedResponse(BaseModel):
    """Response body returned by ``POST /extract`` (202 Accepted).

    Extraction runs in the background; poll ``GET /results/{id}`` until the
    status becomes ``done``.
    """

    id: str = Field(
        ...,
        description="Result id; poll GET /results/{id} until status is 'done'.",
        examples=["a1b2c3d4e5f6"],
    )
    status: str = Field(
        default="pending",
        description="Processing status of the job.",
        examples=["pending"],
    )


class MatchRequest(BaseModel):
    """Request body for ``POST /match``."""

    profile: Profile = Field(
        ..., description="Candidate profile, typically from POST /extract."
    )
    job_description: str = Field(
        ...,
        min_length=1,
        description="Free-text job description to score the profile against.",
        examples=["Senior Python engineer with FastAPI and Kubernetes experience."],
    )


class MatchResponse(BaseModel):
    """Response body returned by ``POST /match``."""

    id: str = Field(
        ...,
        description="Result id; pass to GET /results/{id} to retrieve it later.",
        examples=["a1b2c3d4e5f6"],
    )
    score: int = Field(
        ..., ge=0, le=100, description="Overall match score, 0-100.", examples=[67]
    )
    matched_skills: List[str] = Field(
        default_factory=list, description="Required skills the candidate has."
    )
    missing_skills: List[str] = Field(
        default_factory=list, description="Required skills the candidate lacks."
    )
    rationale: str = Field(
        ..., description="Short human-readable explanation of the score."
    )


class BatchMatchRequest(BaseModel):
    """Request body for ``POST /batch-match``."""

    profiles: List[Profile] = Field(
        ...,
        min_length=1,
        description="Candidate profiles to score against the job description.",
    )
    job_description: str = Field(
        ...,
        min_length=1,
        description="Free-text job description to score every profile against.",
        examples=["Senior Python engineer with FastAPI and Kubernetes experience."],
    )


class ShortlistEntry(BaseModel):
    """One ranked candidate in a batch-match shortlist."""

    rank: int = Field(..., ge=1, description="1-based rank, best match first.")
    name: str = Field(..., description="Candidate name.", examples=["Jane Doe"])
    score: int = Field(..., ge=0, le=100, description="Match score, 0-100.")
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    rationale: str


class BatchMatchResponse(BaseModel):
    """Response body returned by ``POST /batch-match``: a ranked shortlist."""

    id: str = Field(
        ...,
        description="Result id; pass to GET /results/{id} to retrieve it later.",
        examples=["a1b2c3d4e5f6"],
    )
    job_description: str
    shortlist: List[ShortlistEntry] = Field(
        ..., description="Candidates ranked by score, best first."
    )


class ResultResponse(BaseModel):
    """A previously stored extract or match result."""

    id: str
    kind: str = Field(
        ..., description="The kind of result: 'extract' or 'match'.", examples=["extract"]
    )
    status: str = Field(
        ...,
        description="Processing status: 'pending', 'done' or 'failed'.",
        examples=["done"],
    )
    result: Optional[dict] = Field(
        default=None,
        description="The stored result payload; null until status is 'done'.",
    )


class HealthResponse(BaseModel):
    status: str = Field(default="ok", examples=["ok"])


class ErrorResponse(BaseModel):
    """Standard error envelope used across the API."""

    detail: str = Field(
        ...,
        description="Human-readable description of the error.",
        examples=["result not found"],
    )
