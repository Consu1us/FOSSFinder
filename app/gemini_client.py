import json
import logging
import random
import time

import httpx
from google import genai
from google.genai import types

logger = logging.getLogger("fossfinder.gemini")

# Gemini rejects a deadline under 10 seconds. The SDK timeout is milliseconds.
MIN_TIMEOUT_MS = 10_000
REQUEST_TIMEOUT_MS = 60_000
# Stop before starting an attempt that would be rejected for a short deadline.
MIN_ATTEMPT_S = MIN_TIMEOUT_MS / 1000
MAX_ATTEMPTS = 3
# One full attempt fits in each budget. Faster failures can still retry.
PRIMARY_BUDGET_S = 60.0
FALLBACK_BUDGET_S = 60.0
RETRY_BASE_S = 0.4
RETRY_CAP_S = 3.0
RETRY_JITTER_S = 0.25

RETRYABLE_CODES = {429, 500, 503}
RETRYABLE_STATUSES = {"UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"}

BUSY_MESSAGE = (
    "Gemini is busy right now and could not read the resume. "
    "Wait a few seconds and try again."
)
REJECTED_MESSAGE = (
    "Gemini could not read this resume. "
    "Check the API key and model in .env, then try again."
)

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


class GeminiProfileError(Exception):
    """Resume reading failed. The message is safe to show in the UI."""

    def __init__(self, message: str, *, busy: bool):
        super().__init__(message)
        self.busy = busy


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return True
    if "timeout" in exc.__class__.__name__.lower():
        return True
    code = _status_code(exc)
    status = str(getattr(exc, "status", "") or "").upper()
    if code in RETRYABLE_CODES or code in {408, 504}:
        return True
    if status in RETRYABLE_STATUSES or status == "DEADLINE_EXCEEDED":
        return True
    text = str(exc).upper()
    if "UNAVAILABLE" in text or "RESOURCE_EXHAUSTED" in text:
        return True
    return text.startswith(("429 ", "500 ", "503 "))


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _backoff_seconds(failed_attempts: int, rng: random.Random) -> float:
    growth = min(RETRY_CAP_S, RETRY_BASE_S * (2**failed_attempts))
    return growth + rng.uniform(0, RETRY_JITTER_S)


def _as_dict(response) -> dict:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(response, "text", None) or ""
    return json.loads(text)


class GeminiMatcher:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str = "",
        *,
        client=None,
        sleep=None,
        clock=None,
        rng: random.Random | None = None,
    ):
        self._model = (model or "").strip()
        fallback = (fallback_model or "").strip()
        self._fallback_model = "" if not fallback or fallback == self._model else fallback
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=_http_options(REQUEST_TIMEOUT_MS),
        )
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()

    def profile_from_resume(self, resume_text: str) -> dict:
        contents = (
            "Read this resume and extract a technical profile for open-source matching. "
            "skills_listed: only skills written in a skills, technologies, or tools section. "
            "skills_inferred: skills demonstrated by jobs, projects, education, or publications, "
            "and not already copied into skills_listed. "
            "languages and frameworks should be specific tools the resume supports. "
            "domains are fields such as web, data, or mobile. "
            "seniority is one of student, junior, mid, senior, or unspecified. "
            "Leave arrays empty when the resume does not support them. Do not invent skills.\n\n"
            f"{resume_text}"
        )
        try:
            response = self._generate(contents, PROFILE_SCHEMA)
            profile = _as_dict(response)
        except GeminiProfileError:
            raise
        except Exception as exc:
            logger.warning("Gemini profile extraction failed: %s", exc)
            raise GeminiProfileError(
                BUSY_MESSAGE if is_retryable(exc) else REJECTED_MESSAGE,
                busy=is_retryable(exc),
            ) from exc
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
        contents = (
            "Choose the reviewed open-source repositories this person could contribute to. "
            "Use only repositories in the input. Copy full_name exactly. "
            "Prefer overlap with skills_listed and skills_inferred, then a difficulty that fits seniority. "
            "why: two sentences naming the specific skills that transfer and the kind of change they could make. "
            "score: confidence from 0 to 100 that a first contribution is realistic. "
            "Return at most 5 rankings, highest score first. Skip a repository when the overlap is weak.\n\n"
            f"Profile:\n{json.dumps(profile)}\n\nRepositories:\n{json.dumps(compact)}"
        )
        try:
            response = self._generate(contents, RANK_SCHEMA)
        except Exception as exc:
            logger.warning(
                "Gemini ranking failed after retries (%s). Local skill-overlap ranking will be used.",
                exc,
            )
            raise
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

    def _generate(self, contents: str, schema: dict):
        try:
            return self._call_model(self._model, contents, schema, PRIMARY_BUDGET_S)
        except Exception as exc:
            if not is_retryable(exc) or not self._fallback_model:
                raise
            logger.warning(
                "Gemini model %s stayed unavailable. Trying %s.",
                self._model,
                self._fallback_model,
            )
            return self._call_model(
                self._fallback_model, contents, schema, FALLBACK_BUDGET_S
            )

    def _call_model(self, model: str, contents: str, schema: dict, budget_s: float):
        deadline = self._clock() + budget_s
        last_error: BaseException | None = None
        for attempt in range(MAX_ATTEMPTS):
            remaining = deadline - self._clock()
            remaining_ms = int(remaining * 1000)
            if remaining_ms < MIN_TIMEOUT_MS:
                break
            timeout_ms = min(REQUEST_TIMEOUT_MS, remaining_ms)
            try:
                return self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=schema,
                        temperature=0.2,
                        http_options=_http_options(timeout_ms),
                    ),
                )
            except Exception as exc:
                last_error = exc
                if not is_retryable(exc):
                    raise
                logger.warning(
                    "Gemini model %s failed (%s). Attempt %s of %s.",
                    model,
                    exc,
                    attempt + 1,
                    MAX_ATTEMPTS,
                )
                if attempt == MAX_ATTEMPTS - 1:
                    break
                delay = _backoff_seconds(attempt, self._rng)
                if deadline - self._clock() - delay < MIN_ATTEMPT_S:
                    break
                self._sleep(delay)
        if last_error is None:
            last_error = TimeoutError("Gemini did not respond in time.")
        raise last_error


def _http_options(timeout_ms: int) -> types.HttpOptions:
    return types.HttpOptions(
        timeout=timeout_ms,
        retry_options=types.HttpRetryOptions(attempts=1),
    )
