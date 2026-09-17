"""Handling of attacker-controlled document text.

Uploaded CVs and job descriptions are hostile input. Two things actually
contain them, and neither is pattern matching:

1. Structure: the document is only ever passed as *data*, fenced with a
   per-request random marker the author cannot predict, and the system prompt
   says text inside the fence is never an instruction.
2. Output validation: whatever the model returns is treated as untrusted and
   coerced into our schema. A fully hijacked model still cannot put a value in
   the database that we did not validate.

The heuristics below feed logs and metrics. They are not a filter, because a
blocklist of phrasings is trivially bypassed and pretending otherwise would be
worse than not having it.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("docintel.sanitize")

MAX_DOCUMENT_CHARS = 100_000
MAX_JOB_DESCRIPTION_CHARS = 10_000
MAX_NAME_CHARS = 200
MAX_SKILL_CHARS = 64
MAX_SKILLS = 100
MAX_RATIONALE_CHARS = 2_000

# Zero-width and bidirectional overrides: invisible in a text editor, so they
# are used to hide instructions from a human reviewing the document.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
# C0/C1 controls except tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_SUSPICIOUS = [
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above)", re.I), "override_attempt"),
    (re.compile(r"disregard\s+(the\s+)?(previous|prior|above)", re.I), "override_attempt"),
    (re.compile(r"\bsystem\s*prompt\b", re.I), "prompt_probe"),
    (re.compile(r"\byou\s+are\s+now\b", re.I), "persona_switch"),
    (re.compile(r"-{3,}\s*(begin|end)\s+document", re.I), "fence_forgery"),
    (re.compile(r"\b(api[_ -]?key|secret|password|token)\b", re.I), "secret_probe"),
]


@dataclass
class Sanitised:
    text: str
    truncated: bool = False
    signals: List[str] = field(default_factory=list)


def _clean(raw: str, limit: int) -> Sanitised:
    # NFKC first: it folds homoglyphs and full-width forms, so the patterns
    # below cannot be dodged with lookalike characters.
    text = unicodedata.normalize("NFKC", raw)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)

    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    signals = sorted({label for pattern, label in _SUSPICIOUS if pattern.search(text)})
    return Sanitised(text=text, truncated=truncated, signals=signals)


def sanitise_document(raw: str) -> Sanitised:
    result = _clean(raw, MAX_DOCUMENT_CHARS)
    if result.signals or result.truncated:
        logger.warning(
            "suspicious document",
            extra={
                "extra_fields": {
                    "signals": result.signals,
                    "truncated": result.truncated,
                }
            },
        )
    return result


def sanitise_job_description(raw: str) -> Sanitised:
    return _clean(raw, MAX_JOB_DESCRIPTION_CHARS)


def _clamp_str(value, limit: int) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return _CONTROL.sub("", unicodedata.normalize("NFKC", value))[:limit]


def _clamp_skills(value) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    skills = []
    for item in value:
        skill = _clamp_str(item, MAX_SKILL_CHARS).strip().lower()
        if skill and skill not in skills:
            skills.append(skill)
        if len(skills) >= MAX_SKILLS:
            break
    return skills


def _clamp_int(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


def validate_profile(raw) -> dict:
    """Coerce a model response into our schema, discarding anything else."""
    if not isinstance(raw, dict):
        return {"name": "", "skills": [], "years_experience": 0}
    return {
        "name": _clamp_str(raw.get("name"), MAX_NAME_CHARS),
        "skills": _clamp_skills(raw.get("skills")),
        "years_experience": _clamp_int(raw.get("years_experience"), 0, 80),
    }


def validate_match(raw) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "score": _clamp_int(raw.get("score"), 0, 100),
        "matched_skills": _clamp_skills(raw.get("matched_skills")),
        "missing_skills": _clamp_skills(raw.get("missing_skills")),
        "rationale": _clamp_str(raw.get("rationale"), MAX_RATIONALE_CHARS),
    }


def assert_no_egress(payload: str, secrets: List[str]) -> None:
    """Refuse output that echoes something the client must never receive."""
    for secret in secrets:
        if secret and secret in payload:
            raise ValueError("model output contained protected content")
