"""Document extraction + matching.

A real model is slow and expensive, so by default we use a deterministic fake
provider that runs offline. Set LLM_PROVIDER=openai (and OPENAI_API_KEY) to use
the real thing.
"""

import re
import time

from app.config import settings

SKILLS = [
    "python",
    "fastapi",
    "django",
    "flask",
    "docker",
    "kubernetes",
    "k8s",
    "celery",
    "redis",
    "postgres",
    "postgresql",
    "mysql",
    "nginx",
    "aws",
    "gcp",
    "azure",
    "terraform",
    "ci/cd",
    "github actions",
    "rabbitmq",
    "kafka",
    "sql",
    "rest",
    "grpc",
    "graphql",
    "react",
    "typescript",
    "go",
    "rust",
    "java",
    "spring",
    "pytest",
    "linux",
    "bash",
]


class FakeProvider:
    def extract(self, text: str) -> dict:
        time.sleep(settings.llm_latency)  # pretend the model is working
        lowered = text.lower()
        skills = sorted({s for s in SKILLS if s in lowered})
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        name = lines[0][:80] if lines else "Unknown"
        years = 0
        m = re.search(r"(\d+)\s*\+?\s*years", lowered)
        if m:
            years = int(m.group(1))
        return {"name": name, "skills": skills, "years_experience": years}

    def match(self, profile: dict, job_description: str) -> dict:
        time.sleep(settings.llm_latency)
        job = job_description.lower()
        required = sorted({s for s in SKILLS if s in job})
        have = {s.lower() for s in profile.get("skills", [])}
        matched = [s for s in required if s in have]
        score = round(100 * len(matched) / len(required)) if required else 0
        return {
            "score": score,
            "matched_skills": matched,
            "missing_skills": [s for s in required if s not in have],
            "rationale": f"Candidate matches {len(matched)}/{len(required)} required skills.",
        }


class OpenAIProvider:
    def __init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model

    def _chat(self, prompt: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"

    def extract(self, text: str) -> dict:
        import json

        out = self._chat(
            "Extract the candidate profile as JSON with keys name, skills "
            f"(list), years_experience (int) from this CV:\n\n{text}"
        )
        return json.loads(out)

    def match(self, profile: dict, job_description: str) -> dict:
        import json

        out = self._chat(
            "Score this candidate against the job as JSON with keys score "
            f"(0-100), matched_skills, missing_skills, rationale.\n\n"
            f"Profile: {profile}\n\nJob: {job_description}"
        )
        return json.loads(out)


def get_provider():
    if settings.llm_provider == "openai":
        return OpenAIProvider()
    return FakeProvider()
