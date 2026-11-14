import re

_TOKEN = re.compile(r"[a-z0-9+#.]{2,}")
_DIFFICULTY_RANK = {"beginner": 0, "intermediate": 1, "advanced": 2}
_SENIORITY_RANK = {
    "student": 0,
    "junior": 0,
    "unspecified": 1,
    "mid": 1,
    "senior": 2,
    "staff": 2,
    "principal": 2,
}


def profile_terms(profile: dict) -> list[str]:
    values = []
    for key in ("languages", "frameworks", "skills_listed", "skills_inferred", "domains"):
        values.extend(profile.get(key) or [])
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        for token in _TOKEN.findall(str(value).lower()):
            if token in seen:
                continue
            seen.add(token)
            terms.append(token)
    return terms


def shortlist_repos(profile: dict, repos: list[dict], limit: int = 12) -> list[dict]:
    terms = profile_terms(profile)
    seniority = _SENIORITY_RANK.get(str(profile.get("seniority") or "").lower(), 1)
    scored: list[dict] = []
    for repo in repos:
        overlap = _overlap(terms, repo)
        score = _score(overlap, repo, seniority)
        if score <= 0 and terms:
            continue
        copy = dict(repo)
        copy["overlap"] = overlap
        copy["local_score"] = score
        scored.append(copy)
    if not scored:
        scored = [_beginner_copy(repo) for repo in repos if repo.get("difficulty") == "beginner"]
    scored.sort(key=lambda item: item["local_score"], reverse=True)
    return scored[:limit]


def fallback_rank_repos(profile: dict, repos: list[dict], limit: int = 5) -> list[dict]:
    ranked = []
    for repo in repos:
        overlap = repo.get("overlap") or _overlap(profile_terms(profile), repo)
        notes = repo.get("contribution_notes") or "Read the contributing guide before opening a pull request."
        if overlap:
            why = (
                "Your background overlaps this project on "
                + ", ".join(overlap[:4])
                + ". "
                + notes
            )
        else:
            why = notes
        score = int(repo.get("local_score") or 0)
        ranked.append(
            {
                "full_name": repo["full_name"],
                "score": max(0, min(score, 100)),
                "why": why.strip(),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def _beginner_copy(repo: dict) -> dict:
    copy = dict(repo)
    copy["overlap"] = []
    copy["local_score"] = 15
    return copy


def _overlap(terms: list[str], repo: dict) -> list[str]:
    haystack = set()
    for key in ("languages", "skills", "domains"):
        for value in repo.get(key) or []:
            haystack.update(_TOKEN.findall(str(value).lower()))
    haystack.update(_TOKEN.findall(str(repo.get("full_name") or "").lower()))
    return [term for term in terms if term in haystack]


def _score(overlap: list[str], repo: dict, seniority: int) -> int:
    if not overlap:
        return 0
    languages = {token for value in repo.get("languages") or [] for token in _TOKEN.findall(value.lower())}
    score = 18
    for term in overlap:
        score += 28 if term in languages else 12
    difficulty = _DIFFICULTY_RANK.get(repo.get("difficulty"), 1)
    gap = abs(difficulty - seniority)
    if gap == 0:
        score += 10
    elif gap == 1:
        score += 4
    return min(score, 100)
