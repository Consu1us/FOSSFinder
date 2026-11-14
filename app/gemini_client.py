import json
import logging
import math
import random
import time

import httpx
from google import genai
from google.genai import types

logger = logging.getLogger("fossfinder.gemini")

# Gemini rejects a manually set deadline under 10 seconds.
MIN_TIMEOUT_S = 10.0
MIN_TIMEOUT_MS = 10_000
# Structured profile calls often take 10-20s, so the default leaves headroom.
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TIMEOUT_MS = 30_000
# One retry on a model. A fast 503 still gets another try; a hung call does not stack.
MAX_ATTEMPTS = 2
# Primary attempt plus backup model, including one retry when the failure is fast.
OVERALL_BUDGET_S = 60.0
RETRY_BASE_S = 0.4
RETRY_CAP_S = 3.0
RETRY_JITTER_S = 0.25

RETRYABLE_CODES = {429, 500, 503}
RETRYABLE_STATUSES = {"UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"}

BUSY_MESSAGE = (
    "Gemini is busy right now and could not read the resume. "
    "Please wait a few seconds then try again."
)
REJECTED_MESSAGE = (
    "Gemini could not read this resume. "
    "Make sure it's in a format that Gemini can read, then try again."
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
                    "project_description": {"type": "string"},
                },
                "required": ["full_name", "score", "why", "project_description"],
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


def normalize_timeout_seconds(value) -> float:
    """Return a Gemini deadline in seconds. Blank or invalid values use 30s; shorter values become 10s."""
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_TIMEOUT_S
        value = text
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(seconds):
        return DEFAULT_TIMEOUT_S
    if seconds < MIN_TIMEOUT_S:
        return MIN_TIMEOUT_S
    return seconds


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
        timeout_seconds: float | str | None = None,
        client=None,
        sleep=None,
        clock=None,
        rng: random.Random | None = None,
    ):
        self._model = (model or "").strip()
        fallback = (fallback_model or "").strip()
        self._fallback_model = "" if not fallback or fallback == self._model else fallback
        self._timeout_s = normalize_timeout_seconds(timeout_seconds)
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=_http_options(self._client_timeout_ms()),
        )
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self.profile_model = ""
        self.rank_model = ""

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
            response, model_name = self._generate(contents, PROFILE_SCHEMA)
            self.profile_model = model_name
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
                "contribution_workflow": repo.get("contribution_workflow") or "github-pr",
            }
            for repo in repos[:12]
        ]
        contents = (
            "Choose the reviewed open-source repositories this person could contribute to. "
            "Use only repositories in the input. Copy full_name exactly. "
            "Prefer overlap with skills_listed and skills_inferred, then a difficulty that fits seniority. "
            "why: write in the first person, as yourself speaking directly to this person. "
            "Two or three sentences. Name the skills that transfer and the kind of change they could make. "
            "project_description: 2 to 4 sentences on what the project is and what contributing looks like. "
            "Use only the catalog description, languages, skills, domains, difficulty, contribution_notes, "
            "and contribution_workflow. Do not invent maintainers, issue counts, release history, or a "
            "contribution process the catalog does not state. "
            "score: confidence from 0 to 100 that a first contribution is realistic. "
            "don't be afraid to give the user low scores if the overlap is weak"
            "Return at most 5 rankings, highest score first. Skip a repository when the overlap is weak.\n\n"
            f"Profile:\n{json.dumps(profile)}\n\nRepositories:\n{json.dumps(compact)}"
        )
        try:
            response, model_name = self._generate(contents, RANK_SCHEMA)
            self.rank_model = model_name
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
                    "project_description": str(item.get("project_description") or "").strip(),
                }
            )
        cleaned.sort(key=lambda item: item["score"], reverse=True)
        return cleaned[:5]

    def _client_timeout_ms(self) -> int:
        # The client default matches the first request: at least 10s, and inside this call's budget.
        return min(int(self._timeout_s * 1000), int(self._primary_budget() * 1000))

    def _primary_budget(self) -> float:
        if self._fallback_model:
            # Leave a legal 10s deadline for the backup model inside the 60s cap.
            return min(self._timeout_s, OVERALL_BUDGET_S - MIN_TIMEOUT_S)
        return min(self._timeout_s * MAX_ATTEMPTS, OVERALL_BUDGET_S)

    def _fallback_budget(self) -> float:
        if not self._fallback_model:
            return 0.0
        return min(self._timeout_s, OVERALL_BUDGET_S - self._primary_budget())

    def _generate(self, contents: str, schema: dict) -> tuple[object, str]:
        try:
            return (
                self._call_model(self._model, contents, schema, self._primary_budget()),
                self._model,
            )
        except Exception as exc:
            if not is_retryable(exc) or not self._fallback_model:
                raise
            logger.warning(
                "Gemini model %s stayed unavailable. Trying %s.",
                self._model,
                self._fallback_model,
            )
            return (
                self._call_model(
                    self._fallback_model, contents, schema, self._fallback_budget()
                ),
                self._fallback_model,
            )

    def _call_model(self, model: str, contents: str, schema: dict, budget_s: float):
        deadline = self._clock() + budget_s
        last_error: BaseException | None = None
        for attempt in range(MAX_ATTEMPTS):
            remaining_ms = int((deadline - self._clock()) * 1000)
            # Never send Gemini a deadline under 10 seconds; skip the attempt instead.
            if remaining_ms < MIN_TIMEOUT_MS:
                break
            timeout_ms = min(int(self._timeout_s * 1000), remaining_ms)
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
                remaining_after_sleep_ms = int((deadline - self._clock() - delay) * 1000)
                if remaining_after_sleep_ms < MIN_TIMEOUT_MS:
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
