from typing import List, Optional

from pydantic import BaseModel


class Profile(BaseModel):
    name: str
    skills: List[str]
    years_experience: int


class MatchRequest(BaseModel):
    profile: dict
    job_description: str


class MatchResult(BaseModel):
    score: int
    matched_skills: List[str]
    missing_skills: Optional[List[str]] = None
    rationale: str
