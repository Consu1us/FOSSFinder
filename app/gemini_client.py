import json

from google import genai
from google.genai import types

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "skills_listed": {"type": "array", "items": {"type": "string"}},
        "skills_inferred": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
        "frameworks": {"type": "array", "items": {"type": "string"}},
        "domains": {"type": "array", "items": {"type": "string"}},
        "seniority": {"type": "string"},
    },
    "required": [
        "summary",
        "skills_listed",
        "skills_inferred",
        "languages",
        "frameworks",
        "domains",
        "seniority",
    ],
}

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "score": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["full_name", "score", "why"],
            },
        }
    },
    "required": ["rankings"],
}


def _as_dict(response) -> dict:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(response, "text", None) or ""
    return json.loads(text)


class GeminiMatcher:
    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def profile_from_resume(self, resume_text: str) -> dict:
        response = self._client.models.generate_content(
            model=self._model,
            contents=(
                "Read this resume and extract a technical profile for open-source matching. "
                "skills_listed: only skills written in a skills, technologies, or tools section. "
                "skills_inferred: skills demonstrated by jobs, projects, education, or publications, "
                "and not already copied into skills_listed. "
                "languages and frameworks should be specific tools the resume supports. "
                "domains are fields such as web, data, or mobile. "
                "seniority is one of student, junior, mid, senior, or unspecified. "
                "Leave arrays empty when the resume does not support them. Do not invent skills.\n\n"
                f"{resume_text}"
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=PROFILE_SCHEMA,
                temperature=0.2,
            ),
        )
        profile = _as_dict(response)
        for key in ("skills_listed", "skills_inferred", "languages", "frameworks", "domains"):
            values = profile.get(key) or []
            profile[key] = [str(item).strip() for item in values if str(item).strip()][:12]
        profile["summary"] = str(profile.get("summary") or "").strip()
        profile["seniority"] = str(profile.get("seniority") or "unspecified").strip().lower()
        return profile

    def rank_repos(self, profile: dict, repos: list[dict]) -> list[dict]:
        compact = [
            {
                "full_name": repo["full_name"],
                "description": repo.get("description") or "",
                "languages": repo.get("languages") or [],
                "skills": repo.get("skills") or [],
                "domains": repo.get("domains") or [],
                "difficulty": repo.get("difficulty"),
                "contribution_notes": repo.get("contribution_notes") or "",
            }
            for repo in repos[:12]
        ]
        response = self._client.models.generate_content(
            model=self._model,
            contents=(
                "Choose the reviewed open-source repositories this person could contribute to. "
                "Use only repositories in the input. Copy full_name exactly. "
                "Prefer overlap with skills_listed and skills_inferred, then a difficulty that fits seniority. "
                "why: two sentences naming the specific skills that transfer and the kind of change they could make. "
                "score: confidence from 0 to 100 that a first contribution is realistic. "
                "Return at most 5 rankings, highest score first. Skip a repository when the overlap is weak.\n\n"
                f"Profile:\n{json.dumps(profile)}\n\nRepositories:\n{json.dumps(compact)}"
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=RANK_SCHEMA,
                temperature=0.2,
            ),
        )
        rankings = _as_dict(response).get("rankings") or []
        cleaned = []
        for item in rankings:
            full_name = str(item.get("full_name") or "").strip()
            if not full_name:
                continue
            try:
                score = int(item.get("score"))
            except (TypeError, ValueError):
                score = 0
            cleaned.append(
                {
                    "full_name": full_name,
                    "score": max(0, min(score, 100)),
                    "why": str(item.get("why") or "").strip(),
                }
            )
        cleaned.sort(key=lambda item: item["score"], reverse=True)
        return cleaned[:5]
