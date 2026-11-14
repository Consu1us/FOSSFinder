from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection


_store: "AtlasStore | None" = None
_store_key: tuple[str, str] | None = None

# MongoDB deletes cached starter issues about an hour after cached_at.
# The TTL monitor runs about once a minute, so expiry can lag slightly.
STARTER_ISSUE_TTL = timedelta(hours=1)


def get_store(uri: str, db_name: str) -> "AtlasStore":
    global _store, _store_key
    key = (uri, db_name)
    if _store is None or _store_key != key:
        _store = AtlasStore(uri, db_name)
        _store_key = key
    return _store


def cache_is_fresh(cached_at: datetime, now: datetime | None = None) -> bool:
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return cached_at + STARTER_ISSUE_TTL >= current


def _seniority_key(seniority: str) -> str:
    return (seniority or "unspecified").strip().lower() or "unspecified"


class AtlasStore:
    def __init__(self, uri: str, db_name: str):
        self._client = MongoClient(
            uri,
            serverSelectionTimeoutMS=8000,
            tz_aware=True,
            tzinfo=timezone.utc,
        )
        self._db = self._client[db_name]
        self.repos: Collection = self._db["repos"]
        self.issues: Collection = self._db["issues"]
        self.matches: Collection = self._db["matches"]
        self.issue_cache: Collection = self._db["starter_issue_cache"]
        self.repos.create_index("full_name", unique=True)
        self.repos.create_index("reviewed")
        self.issues.create_index("html_url", unique=True)
        self.issues.create_index("repo_full_name")
        self.matches.create_index("created_at")
        self.issue_cache.create_index(
            [("repo_full_name", 1), ("seniority", 1)],
            unique=True,
            name="repo_seniority",
        )
        self.issue_cache.create_index(
            "cached_at",
            expireAfterSeconds=int(STARTER_ISSUE_TTL.total_seconds()),
            name="cached_at_ttl",
        )

    def ensure_catalog(self, repos: list[dict]) -> int:
        operations = []
        for repo in repos:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            payload = {key: value for key, value in repo.items() if value is not None}
            payload["reviewed"] = True
            payload["source"] = "catalog"
            operations.append(UpdateOne({"full_name": full_name}, {"$set": payload}, upsert=True))
        if not operations:
            return 0
        self.repos.bulk_write(operations, ordered=False)
        return len(operations)

    def reviewed_repos(self) -> list[dict]:
        cursor = self.repos.find({"reviewed": True, "source": "catalog"}, {"_id": 0})
        return list(cursor)

    def ping(self) -> None:
        self._client.admin.command("ping")

    def cached_starter_issues(self, full_name: str, seniority: str) -> list[dict] | None:
        seniority = _seniority_key(seniority)
        doc = self.issue_cache.find_one(
            {"repo_full_name": full_name, "seniority": seniority},
            {"_id": 0, "issues": 1, "cached_at": 1},
        )
        if not doc:
            return None
        cached_at = doc.get("cached_at")
        if not isinstance(cached_at, datetime) or not cache_is_fresh(cached_at):
            return None
        issues = doc.get("issues")
        return issues if isinstance(issues, list) else []

    def cache_starter_issues(self, full_name: str, seniority: str, issues: list[dict]) -> None:
        seniority = _seniority_key(seniority)
        self.issue_cache.update_one(
            {"repo_full_name": full_name, "seniority": seniority},
            {
                "$set": {
                    "repo_full_name": full_name,
                    "seniority": seniority,
                    "issues": issues,
                    "cached_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def upsert_issues(self, issues: list[dict]) -> int:
        count = 0
        for issue in issues:
            url = issue.get("html_url")
            if not url:
                continue
            payload = {
                key: value
                for key, value in issue.items()
                if key != "repo" and value is not None
            }
            self.issues.update_one({"html_url": url}, {"$set": payload}, upsert=True)
            count += 1
        return count

    def save_match(self, profile: dict, rankings: list[dict]) -> str:
        result = self.matches.insert_one(
            {
                "profile": profile,
                "rankings": rankings,
                "created_at": datetime.now(timezone.utc),
            }
        )
        return str(result.inserted_id)

    def counts(self) -> dict[str, int]:
        return {
            "repos": self.repos.count_documents({}),
            "issues": self.issues.count_documents({}),
            "matches": self.matches.count_documents({}),
            "starter_issue_cache": self.issue_cache.count_documents({}),
        }
