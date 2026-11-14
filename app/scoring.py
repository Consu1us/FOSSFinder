from datetime import datetime, timezone


def _updated_recently(updated_at: str | None) -> bool:
    if not updated_at:
        return False
    try:
        when = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - when).days <= 120


def fallback_rank(profile: dict, issues: list[dict], limit: int = 8) -> list[dict]:
    languages = {item.lower() for item in profile.get("languages", [])}
    keywords = [item.lower() for item in profile.get("frameworks", []) + profile.get("domains", [])]
    ranked: list[dict] = []
    for issue in issues:
        labels = {label.lower() for label in issue.get("labels", [])}
        repo = issue.get("repo") or {}
        language = (repo.get("language") or "").lower()
        blob = f"{issue.get('title', '')} {issue.get('body', '')} {repo.get('description', '')}".lower()
        score = 20
        reasons: list[str] = []
        if "good first issue" in labels:
            score += 35
            reasons.append("labeled good first issue")
        if "help wanted" in labels:
            score += 15
            reasons.append("maintainers asked for help")
        if language and language in languages:
            score += 25
            reasons.append(f"repo language matches {repo.get('language')}")
        hits = [word for word in keywords if word and word in blob]
        if hits:
            score += 15
            reasons.append("mentions " + ", ".join(hits[:3]))
        if _updated_recently(issue.get("updated_at")):
            score += 10
            reasons.append("updated in the last few months")
        stars = repo.get("stars") or 0
        if 30 <= stars <= 20000:
            score += 8
        ranked.append(
            {
                "issue_url": issue["html_url"],
                "score": min(score, 100),
                "why": "; ".join(reasons) or "Open issue in a public repository.",
                "first_step": "Read the issue and the contributing guide, then comment with a short plan before writing code.",
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]
