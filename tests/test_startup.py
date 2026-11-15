import inspect
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import DIST, app, lifespan, match_resume, repos

ENV_EMPTY = {
    "MONGODB_URI": "",
    "MONGODB_DB": "fossfinder",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "",
    "GEMINI_FALLBACK_MODEL": "",
    "GEMINI_TIMEOUT_SECONDS": "",
    "GITHUB_TOKEN": "",
}
ENV_READY = {
    "MONGODB_URI": "mongodb://127.0.0.1:9",
    "MONGODB_DB": "fossfinder",
    "GEMINI_API_KEY": "test-key",
    "GEMINI_MODEL": "",
    "GEMINI_FALLBACK_MODEL": "",
    "GEMINI_TIMEOUT_SECONDS": "",
    "GITHUB_TOKEN": "",
}


class _CountingStore:
    def __init__(self):
        self.seeds = 0

    def ping(self) -> None:
        return None

    def ensure_catalog(self, catalog: list[dict]) -> int:
        self.seeds += 1
        return len(catalog)

    def reviewed_repos(self) -> list[dict]:
        return [
            {
                "full_name": "a/b",
                "html_url": "https://github.com/a/b",
                "description": "",
                "languages": [],
                "difficulty": "beginner",
            }
        ]

    def counts(self) -> dict[str, int]:
        return {"repos": 1, "issues": 0, "matches": 0, "starter_issue_cache": 0}


class _DownStore:
    def ping(self) -> None:
        return None

    def ensure_catalog(self, catalog: list[dict]) -> int:
        raise TimeoutError("atlas down")

    def reviewed_repos(self) -> list[dict]:
        return []

    def counts(self) -> dict[str, int]:
        return {"repos": 0, "issues": 0, "matches": 0, "starter_issue_cache": 0}


class StartupTests(unittest.TestCase):
    def test_request_handlers_do_not_seed_the_catalog(self):
        self.assertNotIn("ensure_catalog", inspect.getsource(match_resume))
        self.assertNotIn("ensure_catalog", inspect.getsource(repos))
        source = inspect.getsource(getattr(lifespan, "__wrapped__", lifespan))
        self.assertIn("ensure_catalog", source)

    def test_root_and_health_without_mongodb(self):
        with patch.dict(os.environ, ENV_EMPTY):
            with TestClient(app) as client:
                root = client.get("/")
                health = client.get("/api/health")
        self.assertEqual(root.status_code, 200)
        if (DIST / "index.html").is_file():
            self.assertIn("text/html", root.headers["content-type"])
        else:
            self.assertIn("npm run dev", root.text)
            self.assertIn("npm run build", root.text)
            self.assertIn("Vite dev server", root.text)
        body = health.json()
        self.assertEqual(health.status_code, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["atlas"], "skipped")
        self.assertEqual(body["catalog_seed"], "skipped")
        self.assertEqual(body["catalog_seeded"], 0)
        self.assertFalse(body["github_token"])
        self.assertIn("MONGODB_URI", body["missing"])
        self.assertIn("GEMINI_API_KEY", body["missing"])
        self.assertEqual(body["model"], "gemini-3.5-flash")
        self.assertEqual(body["fallback_model"], "")
        self.assertEqual(body["gemini_timeout_seconds"], 30)
        self.assertEqual(body["vector_search"]["status"], "skipped")
        self.assertFalse(body["vector_search"]["index_ready"])
        self.assertEqual(body["vector_search"]["model"], "gemini-embedding-001")
        self.assertEqual(body["vector_search"]["dimensions"], 768)

    def test_catalog_is_seeded_once_at_startup(self):
        store = _CountingStore()
        with patch.dict(os.environ, ENV_READY):
            with patch("app.main.get_store", return_value=store):
                with TestClient(app) as client:
                    health = client.get("/api/health")
                    listed = client.get("/api/repos")
        self.assertEqual(store.seeds, 1)
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        self.assertEqual(health.json()["catalog_seed"], "ok")
        self.assertGreaterEqual(health.json()["catalog_seeded"], 40)
        self.assertEqual(health.json()["atlas"], "ok")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["repos"][0]["full_name"], "a/b")
        self.assertEqual(store.seeds, 1)

    def test_health_stays_up_when_atlas_seed_fails(self):
        with patch.dict(os.environ, ENV_READY):
            with patch("app.main.get_store", return_value=_DownStore()):
                with self.assertLogs("fossfinder", level="ERROR") as captured:
                    with TestClient(app) as client:
                        health = client.get("/api/health")
                        listed = client.get("/api/repos")
        self.assertTrue(any("unreachable" in line.lower() for line in captured.output))
        body = health.json()
        self.assertEqual(health.status_code, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["atlas"], "ok")
        self.assertEqual(body["catalog_seed"], "error: TimeoutError")
        self.assertEqual(body["catalog_seeded"], 0)
        self.assertEqual(listed.status_code, 503)
        self.assertIn("startup", listed.json()["detail"])
