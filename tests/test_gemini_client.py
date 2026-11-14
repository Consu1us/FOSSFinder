import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from google.genai.errors import ClientError, ServerError

from app.gemini_client import (
    BUSY_MESSAGE,
    MAX_ATTEMPTS,
    REJECTED_MESSAGE,
    REQUEST_TIMEOUT_MS,
    GeminiMatcher,
    GeminiProfileError,
    _backoff_seconds,
    is_retryable,
)
from app.main import app
from tests.test_startup import ENV_READY


def _unavailable(message="This model is currently experiencing high demand."):
    return ServerError(503, {"status": "UNAVAILABLE", "message": message})


def _profile_payload():
    return {
        "summary": "Backend engineer",
        "skills_listed": ["Python"],
        "skills_inferred": ["APIs"],
        "languages": ["Python"],
        "frameworks": ["FastAPI"],
        "domains": ["web"],
        "seniority": "Junior",
    }


def _response(payload):
    return SimpleNamespace(parsed=payload, text="")


class ScriptedModels:
    def __init__(self, script):
        self.calls = []
        self._script = list(script)

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        action = self._script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class ScriptedClient:
    def __init__(self, script):
        self.models = ScriptedModels(script)


def _matcher(script, fallback="", rng=None):
    delays = []
    client = ScriptedClient(script)
    matcher = GeminiMatcher(
        "test-key",
        "gemini-3.8-flash",
        fallback,
        client=client,
        sleep=delays.append,
        clock=lambda: 0.0,
        rng=rng or random.Random(0),
    )
    return matcher, client, delays


class RetryClassificationTests(unittest.TestCase):
    def test_transient_statuses_are_retryable(self):
        self.assertTrue(is_retryable(_unavailable()))
        self.assertTrue(
            is_retryable(ClientError(429, {"status": "RESOURCE_EXHAUSTED", "message": "quota"}))
        )
        self.assertTrue(is_retryable(ServerError(500, {"status": "INTERNAL", "message": "boom"})))
        self.assertTrue(is_retryable(TimeoutError("timed out")))
        self.assertTrue(is_retryable(httpx.ReadTimeout("timed out")))
        self.assertTrue(
            is_retryable(
                RuntimeError(
                    "503 UNAVAILABLE. This model is currently experiencing high demand."
                )
            )
        )

    def test_client_errors_are_not_retryable(self):
        self.assertFalse(
            is_retryable(ClientError(400, {"status": "INVALID_ARGUMENT", "message": "bad request"}))
        )


class BackoffTests(unittest.TestCase):
    def test_delay_grows_and_includes_jitter(self):
        first = _backoff_seconds(0, random.Random(1))
        second = _backoff_seconds(1, random.Random(1))
        self.assertGreater(first, 0.4)
        self.assertLess(first, 0.4 + 0.25)
        self.assertGreater(second, first)


class ProfileRetryTests(unittest.TestCase):
    def test_retries_503_then_succeeds(self):
        matcher, client, delays = _matcher([_unavailable(), _response(_profile_payload())])
        profile = matcher.profile_from_resume("Python and FastAPI")
        self.assertEqual(profile["seniority"], "junior")
        self.assertEqual(profile["languages"], ["Python"])
        self.assertEqual(len(client.models.calls), 2)
        self.assertEqual(
            [call["model"] for call in client.models.calls],
            ["gemini-3.8-flash", "gemini-3.8-flash"],
        )
        self.assertEqual(len(delays), 1)
        self.assertEqual(client.models.calls[0]["config"].http_options.timeout, REQUEST_TIMEOUT_MS)
        self.assertEqual(client.models.calls[0]["config"].http_options.retry_options.attempts, 1)

    def test_timeout_then_success(self):
        matcher, client, delays = _matcher(
            [httpx.ReadTimeout("timed out"), _response(_profile_payload())]
        )
        profile = matcher.profile_from_resume("resume text")
        self.assertEqual(profile["summary"], "Backend engineer")
        self.assertEqual(len(client.models.calls), 2)
        self.assertEqual(len(delays), 1)

    def test_falls_back_to_the_backup_model(self):
        script = [_unavailable() for _ in range(MAX_ATTEMPTS)]
        script.append(_response(_profile_payload()))
        matcher, client, delays = _matcher(script, fallback="gemini-2.5-flash")
        profile = matcher.profile_from_resume("resume text")
        self.assertEqual(profile["languages"], ["Python"])
        models = [call["model"] for call in client.models.calls]
        self.assertEqual(models[:MAX_ATTEMPTS], ["gemini-3.8-flash"] * MAX_ATTEMPTS)
        self.assertEqual(models[-1], "gemini-2.5-flash")
        self.assertEqual(len(delays), MAX_ATTEMPTS - 1)

    def test_skips_fallback_when_it_is_unset(self):
        matcher, client, delays = _matcher([_unavailable() for _ in range(MAX_ATTEMPTS)])
        with self.assertRaises(GeminiProfileError) as raised:
            matcher.profile_from_resume("resume text")
        self.assertTrue(raised.exception.busy)
        self.assertEqual(str(raised.exception), BUSY_MESSAGE)
        self.assertEqual(len(client.models.calls), MAX_ATTEMPTS)
        self.assertTrue(all(call["model"] == "gemini-3.8-flash" for call in client.models.calls))
        self.assertEqual(len(delays), MAX_ATTEMPTS - 1)

    def test_400_is_not_retried_or_sent_to_the_fallback(self):
        matcher, client, delays = _matcher(
            [ClientError(400, {"status": "INVALID_ARGUMENT", "message": "bad request"})],
            fallback="gemini-2.5-flash",
        )
        with self.assertRaises(GeminiProfileError) as raised:
            matcher.profile_from_resume("resume text")
        self.assertFalse(raised.exception.busy)
        self.assertEqual(str(raised.exception), REJECTED_MESSAGE)
        self.assertEqual(len(client.models.calls), 1)
        self.assertEqual(delays, [])

    def test_same_fallback_model_is_not_called_twice(self):
        matcher, client, _delays = _matcher(
            [_unavailable() for _ in range(MAX_ATTEMPTS)],
            fallback="gemini-3.8-flash",
        )
        with self.assertRaises(GeminiProfileError):
            matcher.profile_from_resume("resume text")
        self.assertEqual(len(client.models.calls), MAX_ATTEMPTS)


class RankRetryTests(unittest.TestCase):
    def test_ranking_still_raises_after_retries(self):
        matcher, client, delays = _matcher([_unavailable() for _ in range(MAX_ATTEMPTS)])
        with self.assertRaises(ServerError):
            matcher.rank_repos({"languages": ["Python"]}, [{"full_name": "django/django"}])
        self.assertEqual(len(client.models.calls), MAX_ATTEMPTS)
        self.assertEqual(len(delays), MAX_ATTEMPTS - 1)

    def test_ranking_uses_the_fallback_model(self):
        script = [_unavailable() for _ in range(MAX_ATTEMPTS)]
        script.append(
            _response(
                {
                    "rankings": [
                        {"full_name": "django/django", "score": 90, "why": "Python overlap."}
                    ]
                }
            )
        )
        matcher, client, _delays = _matcher(script, fallback="gemini-2.5-flash")
        ranked = matcher.rank_repos({"languages": ["Python"]}, [{"full_name": "django/django"}])
        self.assertEqual(ranked[0]["full_name"], "django/django")
        self.assertEqual(client.models.calls[-1]["model"], "gemini-2.5-flash")


class ClientSetupTests(unittest.TestCase):
    def test_real_client_gets_a_per_request_timeout(self):
        with patch("app.gemini_client.genai.Client") as ctor:
            GeminiMatcher("key", "gemini-3.8-flash", "gemini-2.5-flash")
        options = ctor.call_args.kwargs["http_options"]
        self.assertEqual(ctor.call_args.kwargs["api_key"], "key")
        self.assertEqual(options.timeout, REQUEST_TIMEOUT_MS)
        self.assertEqual(options.retry_options.attempts, 1)


class _QuietStore:
    def ensure_catalog(self, repos):
        return len(repos)


class MatchMessageTests(unittest.TestCase):
    def test_match_shows_the_busy_message(self):
        class BusyMatcher:
            def profile_from_resume(self, resume_text):
                raise GeminiProfileError(BUSY_MESSAGE, busy=True)

        with patch.dict(os.environ, ENV_READY):
            with patch("app.main.get_store", return_value=_QuietStore()):
                with patch("app.main.extract_resume_text", return_value="A resume"):
                    with patch("app.main.GeminiMatcher", return_value=BusyMatcher()):
                        with TestClient(app) as client:
                            response = client.post(
                                "/api/match",
                                files={"resume": ("resume.pdf", b"%PDF-1.4", "application/pdf")},
                            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], BUSY_MESSAGE)
        self.assertNotIn("UNAVAILABLE", response.json()["detail"])
