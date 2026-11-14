from contextlib import contextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.catalog import load_catalog
from app.config import ROOT, get_settings
from app.gemini_client import GeminiMatcher
from app.github_client import GitHubSearch
from app.matching import fallback_rank_repos, shortlist_repos
from app.pdf_text import extract_resume_text
from app.store import get_store

MAX_PDF_BYTES = 8 * 1024 * 1024
STATIC = ROOT / "app" / "static"
DIST = ROOT / "web" / "dist"

app = FastAPI(title="FOSSFinder", version="0.2.0")
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_settings.allowed_hosts)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@contextmanager
def github_session(token: str):
    client = GitHubSearch(token)
    try:
        yield client
    finally:
        client.close()


def _public_repo(repo: dict, ranking: dict, issues: list[dict]) -> dict:
    return {
        "score": ranking["score"],
        "why": ranking["why"],
        "full_name": repo["full_name"],
        "url": repo.get("html_url") or f"https://github.com/{repo['full_name']}",
        "description": repo.get("description") or "",
        "languages": repo.get("languages") or [],
        "difficulty": repo.get("difficulty") or "intermediate",
        "issues": [
            {
                "title": issue["title"],
                "url": issue["html_url"],
                "labels": issue.get("labels") or [],
            }
            for issue in issues
        ],
    }


@app.get("/")
def index():
    built = DIST / "index.html"
    if built.is_file():
        return FileResponse(built)
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    settings = get_settings()
    missing = settings.ready
    atlas = "skipped"
    catalog = 0
    if not missing:
        try:
            store = get_store(settings.mongodb_uri, settings.mongodb_db)
            store.ping()
            atlas = "ok"
            catalog = store.counts()["repos"]
        except Exception as exc:
            atlas = f"error: {exc.__class__.__name__}"
    return {
        "ok": not missing and atlas == "ok",
        "missing": missing,
        "atlas": atlas,
        "catalog_repos": catalog,
        "model": settings.gemini_model,
        "github_token": bool(settings.github_token),
        "public_url": settings.public_url,
    }


@app.get("/api/repos")
def repos(limit: int = 20):
    settings = get_settings()
    if "MONGODB_URI" in settings.ready:
        raise HTTPException(status_code=503, detail="Set MONGODB_URI in .env")
    limit = max(1, min(limit, 80))
    try:
        store = get_store(settings.mongodb_uri, settings.mongodb_db)
        store.ensure_catalog(load_catalog())
        reviewed = store.reviewed_repos()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB Atlas request failed: {exc}") from exc
    return {"counts": store.counts(), "repos": reviewed[:limit]}


@app.post("/api/match")
def match_resume(resume: UploadFile = File(...)):
    settings = get_settings()
    missing = settings.ready
    if missing:
        raise HTTPException(
            status_code=503,
            detail="Missing " + ", ".join(missing) + ". Copy .env.example to .env and fill them in.",
        )
    filename = (resume.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF resume.")
    data = resume.file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="PDF must be 8 MB or smaller.")
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty.")

    try:
        resume_text = extract_resume_text(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read that PDF: {exc}") from exc

    gemini = GeminiMatcher(settings.gemini_api_key, settings.gemini_model)
    try:
        profile = gemini.profile_from_resume(resume_text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini could not read the resume: {exc}") from exc

    try:
        store = get_store(settings.mongodb_uri, settings.mongodb_db)
        seeded = store.ensure_catalog(load_catalog())
        catalog = store.reviewed_repos()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB Atlas request failed: {exc}") from exc
    if not catalog:
        raise HTTPException(status_code=404, detail="The reviewed repository list is empty.")

    shortlist = shortlist_repos(profile, catalog)
    if not shortlist:
        raise HTTPException(
            status_code=404,
            detail="None of the reviewed repositories overlap the skills on this resume.",
        )

    used_fallback = False
    try:
        rankings = gemini.rank_repos(profile, shortlist)
    except Exception:
        rankings = []
    known = {repo["full_name"].lower(): repo for repo in shortlist}
    rankings = [item for item in rankings if item["full_name"].lower() in known]
    if not rankings:
        rankings = fallback_rank_repos(profile, shortlist)
        used_fallback = True

    issues_note = None
    issues_by_repo: dict[str, list[dict]] = {}
    try:
        with github_session(settings.github_token) as github:
            for item in rankings:
                repo = known[item["full_name"].lower()]
                issues_by_repo[repo["full_name"]] = github.starter_issues(
                    repo["full_name"], profile.get("seniority") or "unspecified"
                )
    except RuntimeError as exc:
        issues_note = str(exc)
    except Exception as exc:
        issues_note = f"GitHub issues could not be loaded: {exc}"

    flat_issues = [issue for issues in issues_by_repo.values() for issue in issues]
    try:
        if flat_issues:
            store.upsert_issues(flat_issues)
        match_id = store.save_match(profile, rankings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB Atlas write failed: {exc}") from exc

    recommendations = [
        _public_repo(
            known[item["full_name"].lower()],
            item,
            issues_by_repo.get(known[item["full_name"].lower()]["full_name"], []),
        )
        for item in rankings
        if item["full_name"].lower() in known
    ]
    return {
        "match_id": match_id,
        "profile": profile,
        "used_fallback": used_fallback,
        "issues_note": issues_note,
        "stored": {"repos": seeded, "issues": len(flat_issues)},
        "recommendations": recommendations,
    }
