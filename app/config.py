from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
import os

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
    for name in (host, bare, f"www.{bare}", "localhost", "127.0.0.1"):
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
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        or "gemini-3.8-flash",
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        public_url=public_url,
        allowed_hosts=allowed_hosts,
        cors_origins=cors_origins,
    )
