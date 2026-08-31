"""Mapping between the provider's dicts and the normalised tables.

Only stages ORM objects, so it works with the API's async session and the
worker's sync one alike.
"""

from typing import Optional

from app.models import Match, Profile, ProfileSkill, Result


def build_profile(result_id: str, profile: dict) -> Profile:
    skills = profile.get("skills") or []
    return Profile(
        result_id=result_id,
        name=(profile.get("name") or "")[:200] or None,
        years_experience=int(profile.get("years_experience") or 0),
        skills=[ProfileSkill(skill=s[:64]) for s in dict.fromkeys(skills)],
    )


def build_match(
    result_id: str,
    job_description: str,
    result: dict,
    *,
    rank: Optional[int] = None,
    candidate_name: Optional[str] = None,
) -> Match:
    return Match(
        result_id=result_id,
        rank=rank,
        candidate_name=(candidate_name or None) and candidate_name[:200],
        score=int(result.get("score") or 0),
        job_description=job_description,
        rationale=result.get("rationale") or "",
        matched_skills=result.get("matched_skills") or [],
        missing_skills=result.get("missing_skills") or [],
    )


def profile_to_dict(profile: Profile) -> dict:
    return {
        "name": profile.name or "",
        "skills": sorted(s.skill for s in profile.skills),
        "years_experience": profile.years_experience,
    }


def matches_to_dict(result: Result) -> Optional[dict]:
    """Rebuild the API-visible payload for a match / batch_match result."""
    rows = sorted(result.matches, key=lambda m: (m.rank or 0, m.id))
    if not rows:
        return None

    if result.kind == "match":
        m = rows[0]
        return {
            "score": m.score,
            "matched_skills": m.matched_skills or [],
            "missing_skills": m.missing_skills or [],
            "rationale": m.rationale,
        }

    return {
        "job_description": rows[0].job_description,
        "shortlist": [
            {
                "rank": m.rank,
                "name": m.candidate_name or "Unknown",
                "score": m.score,
                "matched_skills": m.matched_skills or [],
                "missing_skills": m.missing_skills or [],
                "rationale": m.rationale,
            }
            for m in rows
        ],
    }
