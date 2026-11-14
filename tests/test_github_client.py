import unittest

import httpx

from app.github_client import GitHubSearch


def _github(token: str, handler) -> GitHubSearch:
    github = GitHubSearch(token)
    github._client.close()
    github._client = httpx.Client(transport=httpx.MockTransport(handler))
    return github


class GitHubTokenTests(unittest.TestCase):
    def test_bearer_token_is_attached_when_set(self):
        github = GitHubSearch("ghp_example")
        try:
            self.assertEqual(github._client.headers.get("Authorization"), "Bearer ghp_example")
        finally:
            github.close()

    def test_authorization_header_is_omitted_without_a_token(self):
        github = GitHubSearch("")
        try:
            self.assertFalse(github._client.headers.get("Authorization"))
        finally:
            github.close()

    def test_unauthenticated_rate_limit_names_the_missing_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "API rate limit exceeded"})

        github = _github("", handler)
        try:
            with self.assertRaises(RuntimeError) as raised:
                github.starter_issues("owner/name", "junior")
        finally:
            github.close()
        message = str(raised.exception)
        self.assertIn("GITHUB_TOKEN", message)
        self.assertIn("not set", message)
        self.assertIn("60", message)
        self.assertIn(".env", message)

    def test_authenticated_rate_limit_asks_you_to_wait(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "secondary rate limit"})

        github = _github("ghp_example", handler)
        try:
            with self.assertRaises(RuntimeError) as raised:
                github.starter_issues("owner/name", "senior")
        finally:
            github.close()
        message = str(raised.exception)
        self.assertIn("Wait a minute", message)
        self.assertNotIn("not set", message)

    def test_rejected_token_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        github = _github("ghp_expired", handler)
        try:
            with self.assertRaises(RuntimeError) as raised:
                github.starter_issues("owner/name", "mid")
        finally:
            github.close()
        self.assertIn("rejected GITHUB_TOKEN", str(raised.exception))

    def test_starter_issues_skip_pull_requests(self):
        payload = [
            {
                "number": 7,
                "title": "Fix the docs",
                "body": "Please   fix the docs.",
                "labels": [{"name": "good first issue"}],
                "html_url": "https://github.com/owner/name/issues/7",
                "state": "open",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "pull_request": {"url": "https://api.github.com/repos/owner/name/pulls/8"},
                "number": 8,
                "title": "A pull request",
                "html_url": "https://github.com/owner/name/pull/8",
                "labels": [],
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("labels"), "good first issue")
            return httpx.Response(200, json=payload)

        github = _github("", handler)
        try:
            issues = github.starter_issues("owner/name", "junior", limit=2)
        finally:
            github.close()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["title"], "Fix the docs")
        self.assertEqual(issues[0]["body"], "Please fix the docs.")
        self.assertEqual(issues[0]["repo_full_name"], "owner/name")
        self.assertEqual(issues[0]["labels"], ["good first issue"])


class IssueAvailabilityTests(unittest.TestCase):
    def test_disabled_issues_stop_after_one_request(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(410, json={"message": "Issues are disabled for this repo"})

        github = _github("", handler)
        try:
            issues = github.starter_issues("torvalds/linux", "senior")
        finally:
            github.close()
        self.assertEqual(issues, [])
        self.assertEqual(calls["count"], 1)

    def test_bug_label_ignores_pull_requests(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("labels"), "bug")
            return httpx.Response(
                200,
                json=[{"number": 1, "pull_request": {"url": "https://example.test/pull/1"}}],
            )

        github = _github("", handler)
        try:
            self.assertFalse(github.has_open_bug_issues("owner/name"))
        finally:
            github.close()

    def test_contributing_guide_prefers_the_community_profile(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith("/community/profile"))
            return httpx.Response(
                200,
                json={
                    "files": {
                        "contributing": {
                            "html_url": "https://github.com/a/b/blob/main/CONTRIBUTING.md"
                        }
                    }
                },
            )

        github = _github("", handler)
        try:
            url = github.contributing_guide_url("a/b")
        finally:
            github.close()
        self.assertEqual(url, "https://github.com/a/b/blob/main/CONTRIBUTING.md")

    def test_contributing_guide_checks_known_paths(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/community/profile"):
                return httpx.Response(200, json={"files": {"contributing": None}})
            if url.endswith("/contents/CONTRIBUTING.md"):
                return httpx.Response(404, json={"message": "Not Found"})
            if url.endswith("/contents/.github/CONTRIBUTING.md"):
                return httpx.Response(
                    200,
                    json={"html_url": "https://github.com/a/b/blob/main/.github/CONTRIBUTING.md"},
                )
            return httpx.Response(404, json={"message": "Not Found"})

        github = _github("", handler)
        try:
            url = github.contributing_guide_url("a/b")
        finally:
            github.close()
        self.assertEqual(url, "https://github.com/a/b/blob/main/.github/CONTRIBUTING.md")
