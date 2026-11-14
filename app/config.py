from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os

from app.gemini_client import normalize_timeout_seconds

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    mongodb_db: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    gemini_timeout_seconds: float
    github_token: str

    @property
    def ready(self) -> list[str]:
        missing = []
        if not self.mongodb_uri:
            missing.append("MONGODB_URI")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        return missing


def get_settings() -> Settings:
    return Settings(
        mongodb_uri=os.getenv("MONGODB_URI", "").strip(),
        mongodb_db=os.getenv("MONGODB_DB", "fossfinder").strip() or "fossfinder",
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        or "gemini-3.8-flash",
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "").strip(),
        gemini_timeout_seconds=normalize_timeout_seconds(os.getenv("GEMINI_TIMEOUT_SECONDS", "")),
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
    )
