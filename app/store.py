from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.operations import SearchIndexModel


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
        self.guides: Collection = self._db["contributing_guides"]
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
        self.guides.create_index("repo_full_name", unique=True)
        self.guides.create_index(
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
        cursor = self.repos.find(
            {"reviewed": True, "source": "catalog"},
            {"_id": 0, "embedding": 0},
        )
        return list(cursor)

    def embedding_records(self) -> dict[str, dict]:
        stored = set(
            self.repos.distinct(
                "full_name",
                {"reviewed": True, "source": "catalog", "embedding": {"$type": "array"}},
            )
        )
        cursor = self.repos.find(
            {"reviewed": True, "source": "catalog", "embedding_hash": {"$exists": True}},
            {
                "_id": 0,
                "full_name": 1,
                "embedding_hash": 1,
                "embedding_model": 1,
                "embedding_dimensions": 1,
            },
        )
        records = {}
        for doc in cursor:
            name = doc.get("full_name")
            if not name:
                continue
            records[name] = {
                "embedding_hash": doc.get("embedding_hash") or "",
                "embedding_model": doc.get("embedding_model") or "",
                "embedding_dimensions": doc.get("embedding_dimensions") or 0,
                "has_embedding": name in stored,
            }
        return records

    def save_embeddings(self, rows: list[dict]) -> int:
        operations = []
        for row in rows:
            full_name = row.get("full_name")
            embedding = row.get("embedding")
            if not full_name or not isinstance(embedding, list) or not embedding:
                continue
            operations.append(
                UpdateOne(
                    {"full_name": full_name},
                    {
                        "$set": {
                            "embedding": embedding,
                            "embedding_hash": row.get("embedding_hash") or "",
                            "embedding_model": row.get("embedding_model") or "",
                            "embedding_dimensions": row.get("embedding_dimensions") or len(embedding),
                        }
                    },
                )
            )
        if not operations:
            return 0
        self.repos.bulk_write(operations, ordered=False)
        return len(operations)

    def embedded_repo_count(self) -> int:
        return self.repos.count_documents(
            {"reviewed": True, "source": "catalog", "embedding": {"$type": "array"}}
        )

    def vector_index_state(self, name: str) -> dict:
        try:
            indexes = list(self.repos.list_search_indexes())
        except Exception as exc:
            return {
                "exists": False,
                "status": f"error: {exc.__class__.__name__}",
                "index_ready": False,
            }
        for index in indexes:
            if index.get("name") != name:
                continue
            status = str(index.get("status") or "unknown")
            ready = bool(index.get("queryable")) or status.upper() in {"READY", "STEADY", "ACTIVE"}
            return {"exists": True, "status": status, "index_ready": ready}
        return {"exists": False, "status": "missing", "index_ready": False}

    def ensure_vector_index(self, name: str, definition: dict) -> dict:
        current = self.vector_index_state(name)
        if current["exists"] and not str(current["status"]).startswith("error:"):
            return current
        self.repos.create_search_index(
            SearchIndexModel(definition=definition, name=name, type="vectorSearch")
        )
        return {"exists": True, "status": "PENDING", "index_ready": False}

    def vector_candidates(self, query: list[float], index_name: str, limit: int = 12) -> list[dict]:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": "embedding",
                    "queryVector": query,
                    "numCandidates": max(limit * 10, 100),
                    "limit": limit,
                    "filter": {"reviewed": True, "source": "catalog"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "full_name": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        return list(self.repos.aggregate(pipeline))

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

    def cached_contributing_guide(self, full_name: str) -> str | None:
        doc = self.guides.find_one(
            {"repo_full_name": full_name},
            {"_id": 0, "url": 1, "cached_at": 1},
        )
        if not doc:
            return None
        cached_at = doc.get("cached_at")
        if not isinstance(cached_at, datetime) or not cache_is_fresh(cached_at):
            return None
        url = doc.get("url")
        return url if isinstance(url, str) else ""

    def cache_contributing_guide(self, full_name: str, url: str) -> None:
        self.guides.update_one(
            {"repo_full_name": full_name},
            {
                "$set": {
                    "repo_full_name": full_name,
                    "url": url or "",
                    "cached_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def record_match(self, repo_names: list[str]) -> str:
        names = []
        seen: set[str] = set()
        for name in repo_names:
            cleaned = str(name or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            names.append(cleaned)
        result = self.matches.insert_one(
            {
                "created_at": datetime.now(timezone.utc),
                "recommendation_count": len(names),
                "repos": names,
            }
        )
        return str(result.inserted_id)

    def counts(self) -> dict[str, int]:
        return {
            "repos": self.repos.count_documents({}),
            "issues": self.issues.count_documents({}),
            "matches": self.matches.count_documents({}),
            "starter_issue_cache": self.issue_cache.count_documents({}),
            "contributing_guides": self.guides.count_documents({}),
        }
