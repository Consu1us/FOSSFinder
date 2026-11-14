import re

_TERM = re.compile(r"[^A-Za-z0-9+#.\- ]+")
_LANGUAGE_ALIASES = {
    "js": "JavaScript",
    "node": "JavaScript",
    "nodejs": "JavaScript",
    "ts": "TypeScript",
    "py": "Python",
    "golang": "Go",
    "csharp": "C#",
    "cpp": "C++",
    "rb": "Ruby",
    "rs": "Rust",
    "kt": "Kotlin",
    "sh": "Shell",
}


def clean_term(value: str) -> str:
    cleaned = _TERM.sub("", value).strip()
    return re.sub(r"\s+", " ", cleaned)[:40]


def normalize_language(value: str) -> str:
    term = clean_term(value)
    return _LANGUAGE_ALIASES.get(term.lower(), term)


def unique_terms(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        term = clean_term(value)
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        kept.append(term)
        if len(kept) == limit:
            break
    return kept


def issue_queries(languages: list[str], keywords: list[str]) -> list[str]:
    queries = [
        f'is:issue is:open is:public archived:false label:"good first issue" language:"{language}"'
        for language in unique_terms([normalize_language(item) for item in languages], 3)
    ]
    for keyword in unique_terms(keywords, 1):
        queries.append(
            f'is:issue is:open is:public archived:false label:"help wanted" "{keyword}"'
        )
    if not queries:
        queries.append(
            'is:issue is:open is:public archived:false label:"good first issue"'
        )
    return queries[:4]


def repo_queries(languages: list[str]) -> list[str]:
    queries = [
        f"is:public archived:false good-first-issues:>=2 language:\"{language}\""
        for language in unique_terms([normalize_language(item) for item in languages], 3)
    ]
    if not queries:
        queries.append("is:public archived:false good-first-issues:>=5")
    return queries[:3]
