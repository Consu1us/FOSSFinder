from datetime import datetime, timezone

import httpx

GITHUB_API = "https://api.github.com"
USER_AGENT = "FOSSFinder"


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _excerpt(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    return compact[:limit]


class GitHubSearch:
    def __init__(self, token: str):
        self._client = httpx.Client(
            headers=_headers(token),
            timeout=20.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _search(self, path: str, query: str, per_page: int) -> list[dict]:
        response = self._client.get(
            f"{GITHUB_API}{path}",
            params={"q": query, "sort": "updated", "order": "desc", "per_page": per_page},
        )
        if response.status_code == 403:
            raise RuntimeError(
                "GitHub rate limit reached. Add a GITHUB_TOKEN to .env and try again."
            )
        if response.status_code == 422:
            return []
        response.raise_for_status()
        return response.json().get("items", [])

    def starter_issues(self, full_name: str, seniority: str, limit: int = 2) -> list[dict]:
        level = (seniority or "unspecified").lower()
        if level in {"senior", "staff", "principal"}:
            labels = ["help wanted", "good first issue", ""]
        elif level == "mid":
            labels = ["good first issue", "help wanted", ""]
        else:
            labels = ["good first issue", "good-first-issue", "help wanted"]
        collected: list[dict] = []
        seen: set[str] = set()
        for label in labels:
            if len(collected) >= limit:
                break
            params: dict[str, str | int] = {"state": "open", "per_page": 5, "sort": "updated"}
            if label:
                params["labels"] = label
            response = self._client.get(f"{GITHUB_API}/repos/{full_name}/issues", params=params)
            if response.status_code == 404:
                return collected
            if response.status_code == 403:
                raise RuntimeError(
                    "GitHub rate limit reached. Add a GITHUB_TOKEN to .env and try again."
                )
            if response.status_code >= 400:
                continue
            payload = response.json()
            if not isinstance(payload, list):
                continue
            for item in payload:
                if item.get("pull_request"):
                    continue
                url = item.get("html_url")
                if not url or url in seen:
                    continue
                seen.add(url)
                collected.append(
                    {
                        "repo_full_name": full_name,
                        "number": item.get("number"),
                        "title": item.get("title") or "",
                        "body": _excerpt(item.get("body"), 280),
                        "labels": [
                            name.get("name")
                            for name in item.get("labels") or []
                            if isinstance(name, dict) and name.get("name")
                        ],
                        "html_url": url,
                        "state": item.get("state") or "open",
                        "updated_at": item.get("updated_at"),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                if len(collected) >= limit:
                    break
            if collected and label:
                break
        return collected

    def search_repos(self, query: str) -> list[dict]:
        fetched_at = datetime.now(timezone.utc).isoformat()
        repos = []
        for item in self._search("/search/repositories", query, 8):
            repos.append(
                {
                    "full_name": item.get("full_name"),
                    "html_url": item.get("html_url"),
                    "description": item.get("description") or "",
                    "language": item.get("language"),
                    "topics": item.get("topics") or [],
                    "stars": item.get("stargazers_count") or 0,
                    "open_issues": item.get("open_issues_count") or 0,
                    "updated_at": item.get("updated_at"),
                    "fetched_at": fetched_at,
                }
            )
        return [repo for repo in repos if repo["full_name"]]

    def search_issues(self, query: str, repos_by_name: dict[str, dict]) -> list[dict]:
        fetched_at = datetime.now(timezone.utc).isoformat()
        issues = []
        for item in self._search("/search/issues", query, 8):
            if item.get("pull_request"):
                continue
            repo_url = item.get("repository_url") or ""
            full_name = repo_url.removeprefix(f"{GITHUB_API}/repos/")
            if not full_name or "/" not in full_name:
                continue
            repo = repos_by_name.get(full_name) or {
                "full_name": full_name,
                "html_url": f"https://github.com/{full_name}",
                "description": "",
                "language": None,
                "topics": [],
                "stars": None,
                "open_issues": None,
                "updated_at": None,
                "fetched_at": fetched_at,
            }
            issues.append(
                {
                    "repo_full_name": full_name,
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "body": _excerpt(item.get("body")),
                    "labels": [
                        label.get("name")
                        for label in item.get("labels") or []
                        if label.get("name")
                    ],
                    "html_url": item.get("html_url"),
                    "state": item.get("state") or "open",
                    "updated_at": item.get("updated_at"),
                    "fetched_at": fetched_at,
                    "repo": repo,
                }
            )
        return [issue for issue in issues if issue["html_url"]]
