from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
import os

from app.gemini_client import normalize_timeout_seconds
from app.vectors import DEFAULT_EMBEDDING_MODEL, DEFAULT_VECTOR_INDEX

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


DEFAULT_PUBLIC_URL = "https://fossfinder.work"


def site_access(public_url: str) -> tuple[str, list[str], list[str]]:
    raw = (public_url or DEFAULT_PUBLIC_URL).strip().rstrip("/")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "fossfinder.work").lower()
    scheme = parsed.scheme or "https"
    bare = host[4:] if host.startswith("www.") else host
    names = []
    for name in (host, bare, f"www.{bare}", "localhost", "127.0.0.1", "testserver"):
        if name not in names:
            names.append(name)
    origins = [f"http://{local}" for local in ("127.0.0.1:5173", "localhost:5173", "127.0.0.1:8000", "localhost:8000")]
    for name in names:
        if name in {"localhost", "127.0.0.1"}:
            continue
        origin = f"{scheme}://{name}"
        if origin not in origins:
            origins.append(origin)
    return f"{scheme}://{bare}", names, origins


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    mongodb_db: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    gemini_timeout_seconds: float
    gemini_embedding_model: str
    vector_index_name: str
    github_token: str
    public_url: str
    allowed_hosts: list[str]
    cors_origins: list[str]

    @property
    def ready(self) -> list[str]:
        missing = []
        if not self.mongodb_uri:
            missing.append("MONGODB_URI")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        return missing


def get_settings() -> Settings:
    public_url, allowed_hosts, cors_origins = site_access(os.getenv("PUBLIC_URL", ""))
    return Settings(
        mongodb_uri=os.getenv("MONGODB_URI", "").strip(),
        mongodb_db=os.getenv("MONGODB_DB", "fossfinder").strip() or "fossfinder",
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
        or "gemini-3.5-flash",
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "").strip(),
        gemini_timeout_seconds=normalize_timeout_seconds(os.getenv("GEMINI_TIMEOUT_SECONDS", "")),
        gemini_embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
        or DEFAULT_EMBEDDING_MODEL,
        vector_index_name=os.getenv("ATLAS_VECTOR_INDEX", DEFAULT_VECTOR_INDEX).strip()
        or DEFAULT_VECTOR_INDEX,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        public_url=public_url,
        allowed_hosts=allowed_hosts,
        cors_origins=cors_origins,
    )
