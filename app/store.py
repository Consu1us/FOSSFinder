from datetime import datetime, timezone

from pymongo import MongoClient
from pymongo.collection import Collection


_store: "AtlasStore | None" = None
_store_key: tuple[str, str] | None = None


def get_store(uri: str, db_name: str) -> "AtlasStore":
    global _store, _store_key
    key = (uri, db_name)
    if _store is None or _store_key != key:
        _store = AtlasStore(uri, db_name)
        _store_key = key
    return _store


class AtlasStore:
    def __init__(self, uri: str, db_name: str):
        self._client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        self._db = self._client[db_name]
        self.repos: Collection = self._db["repos"]
        self.issues: Collection = self._db["issues"]
        self.matches: Collection = self._db["matches"]
        self.repos.create_index("full_name", unique=True)
        self.repos.create_index("reviewed")
        self.issues.create_index("html_url", unique=True)
        self.issues.create_index("repo_full_name")
        self.matches.create_index("created_at")

    def ensure_catalog(self, repos: list[dict]) -> int:
        count = 0
        for repo in repos:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            payload = {key: value for key, value in repo.items() if value is not None}
            payload["reviewed"] = True
            payload["source"] = "catalog"
            self.repos.update_one({"full_name": full_name}, {"$set": payload}, upsert=True)
            count += 1
        return count

    def reviewed_repos(self) -> list[dict]:
        cursor = self.repos.find({"reviewed": True, "source": "catalog"}, {"_id": 0})
        return list(cursor)

    def ping(self) -> None:
        self._client.admin.command("ping")

    def upsert_repos(self, repos: list[dict]) -> int:
        count = 0
        for repo in repos:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            payload = {key: value for key, value in repo.items() if value is not None}
            payload.pop("repo", None)
            self.repos.update_one({"full_name": full_name}, {"$set": payload}, upsert=True)
            count += 1
        return count

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
        }

    def recent_repos(self, limit: int = 20) -> list[dict]:
        cursor = self.repos.find({}, {"_id": 0}).sort("fetched_at", -1).limit(limit)
        return list(cursor)
