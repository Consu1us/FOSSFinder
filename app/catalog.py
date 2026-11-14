import json

from app.config import ROOT

CATALOG_PATH = ROOT / "data" / "catalog.json"
REQUIRED = (
    "full_name",
    "html_url",
    "description",
    "languages",
    "skills",
    "domains",
    "difficulty",
    "contribution_notes",
)


def load_catalog() -> list[dict]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("The reviewed catalog is empty.")
    repos = []
    seen: set[str] = set()
    for entry in raw:
        full_name = str(entry.get("full_name") or "").strip()
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        repos.append(
            {
                "full_name": full_name,
                "html_url": str(entry.get("html_url") or f"https://github.com/{full_name}"),
                "description": str(entry.get("description") or "").strip(),
                "languages": _strings(entry.get("languages")),
                "skills": [item.lower() for item in _strings(entry.get("skills"))],
                "domains": [item.lower() for item in _strings(entry.get("domains"))],
                "difficulty": str(entry.get("difficulty") or "intermediate").strip().lower(),
                "contribution_notes": str(entry.get("contribution_notes") or "").strip(),
                "reviewed": True,
                "source": "catalog",
            }
        )
    return repos


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
