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


def rate_limit_message(has_token: bool) -> str:
    if not has_token:
        return (
            "GitHub rate limit reached because GITHUB_TOKEN is not set. "
            "Unauthenticated clients get about 60 REST requests per hour "
            "(about 10 requests per minute on the search API), and one match "
            "can make up to 15 starter-issue calls. "
            "Create a token with public repository read access at "
            "https://github.com/settings/tokens, add it to .env as GITHUB_TOKEN, and try again."
        )
    return "GitHub rate limit reached. Wait a minute and try again."


def _excerpt(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    return compact[:limit]


class GitHubSearch:
    def __init__(self, token: str):
        self._token = token
        self._client = httpx.Client(
            headers=_headers(token),
            timeout=20.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _raise_if_rate_limited(self, response: httpx.Response) -> None:
        if response.status_code in {403, 429}:
            raise RuntimeError(rate_limit_message(bool(self._token)))
        if response.status_code == 401:
            raise RuntimeError(
                "GitHub rejected GITHUB_TOKEN. Check that the token is valid and "
                "can read public repositories, then update .env."
            )

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
            self._raise_if_rate_limited(response)
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
