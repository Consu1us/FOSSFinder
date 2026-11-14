import logging
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.catalog import load_catalog
from app.config import ROOT, get_settings
from app.gemini_client import GeminiMatcher, GeminiProfileError
from app.github_client import GitHubSearch
from app.matching import fallback_rank_repos, shortlist_repos
from app.pdf_text import extract_resume_text
from app.store import AtlasStore, get_store

MAX_PDF_BYTES = 8 * 1024 * 1024
DIST = ROOT / "web" / "dist"
UI_HINT = (
    "FOSSFinder API is running.\n"
    "\n"
    "The React app is not built yet. From the web directory, run one of these:\n"
    "  npm run dev    start the Vite dev server at http://127.0.0.1:5173\n"
    "  npm run build  build the app, then reload this page\n"
)

logger = logging.getLogger("fossfinder")
startup_status: dict[str, str | int] = {"catalog_seed": "skipped", "catalog_seeded": 0}


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    settings = get_settings()
    if not settings.mongodb_uri:
        startup_status["catalog_seed"] = "skipped"
        startup_status["catalog_seeded"] = 0
        logger.info("MONGODB_URI is not set; skipping catalog seed.")
    else:
        try:
            store = get_store(settings.mongodb_uri, settings.mongodb_db)
            seeded = store.ensure_catalog(load_catalog())
        except Exception as exc:
            startup_status["catalog_seed"] = f"error: {exc.__class__.__name__}"
            startup_status["catalog_seeded"] = 0
            logger.exception(
                "MongoDB Atlas was unreachable during catalog seed. The API will stay up."
            )
        else:
            startup_status["catalog_seed"] = "ok"
            startup_status["catalog_seeded"] = seeded
            logger.info("Seeded %s reviewed repositories into MongoDB.", seeded)
    app.state.catalog_seed = startup_status["catalog_seed"]
    app.state.catalog_seeded = startup_status["catalog_seeded"]
    yield


app = FastAPI(title="FOSSFinder", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
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


def _startup_seed_error() -> HTTPException | None:
    seed = str(startup_status["catalog_seed"])
    if not seed.startswith("error:"):
        return None
    return HTTPException(
        status_code=503,
        detail=(
            "MongoDB Atlas was unreachable when the reviewed catalog was seeded at startup "
            f"({seed}). The API is up. Check the cluster, then restart to load data/catalog.json."
        ),
    )


def _read_cached_issues(store: AtlasStore, full_name: str, seniority: str) -> list[dict] | None:
    try:
        return store.cached_starter_issues(full_name, seniority)
    except Exception:
        logger.warning("Starter-issue cache read failed for %s", full_name, exc_info=True)
        return None


def _write_cached_issues(
    store: AtlasStore, full_name: str, seniority: str, issues: list[dict]
) -> None:
    try:
        store.cache_starter_issues(full_name, seniority, issues)
    except Exception:
        logger.warning("Starter-issue cache write failed for %s", full_name, exc_info=True)


@app.get("/")
def index():
    built = DIST / "index.html"
    if built.is_file():
        return FileResponse(built)
    return PlainTextResponse(UI_HINT)


@app.get("/api/health")
def health():
    settings = get_settings()
    missing = settings.ready
    atlas = "skipped"
    catalog = 0
    if settings.mongodb_uri:
        try:
            store = get_store(settings.mongodb_uri, settings.mongodb_db)
            store.ping()
            atlas = "ok"
            catalog = store.counts()["repos"]
        except Exception as exc:
            atlas = f"error: {exc.__class__.__name__}"
    return {
        "ok": not missing and atlas == "ok" and startup_status["catalog_seed"] == "ok",
        "missing": missing,
        "atlas": atlas,
        "catalog_seed": startup_status["catalog_seed"],
        "catalog_seeded": startup_status["catalog_seeded"],
        "catalog_repos": catalog,
        "model": settings.gemini_model,
        "fallback_model": settings.gemini_fallback_model,
        "github_token": bool(settings.github_token),
    }


@app.get("/api/repos")
def repos(limit: int = 20):
    settings = get_settings()
    if "MONGODB_URI" in settings.ready:
        raise HTTPException(status_code=503, detail="Set MONGODB_URI in .env")
    limit = max(1, min(limit, 80))
    try:
        store = get_store(settings.mongodb_uri, settings.mongodb_db)
        reviewed = store.reviewed_repos()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB Atlas request failed: {exc}") from exc
    if not reviewed:
        failed = _startup_seed_error()
        if failed is not None:
            raise failed
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

    gemini = GeminiMatcher(
        settings.gemini_api_key,
        settings.gemini_model,
        settings.gemini_fallback_model,
    )
    try:
        profile = gemini.profile_from_resume(resume_text)
    except GeminiProfileError as exc:
        raise HTTPException(status_code=503 if exc.busy else 502, detail=str(exc)) from exc

    try:
        store = get_store(settings.mongodb_uri, settings.mongodb_db)
        catalog = store.reviewed_repos()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB Atlas request failed: {exc}") from exc
    if not catalog:
        failed = _startup_seed_error()
        if failed is not None:
            raise failed
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
    seniority = str(profile.get("seniority") or "unspecified").strip().lower() or "unspecified"
    misses: list[str] = []
    seen: set[str] = set()
    for item in rankings:
        repo = known[item["full_name"].lower()]
        name = repo["full_name"]
        if name in seen:
            continue
        seen.add(name)
        cached = _read_cached_issues(store, name, seniority)
        if cached is not None:
            issues_by_repo[name] = cached
            continue
        misses.append(name)

    if misses:
        try:
            with github_session(settings.github_token) as github:
                for name in misses:
                    issues = github.starter_issues(name, seniority)
                    issues_by_repo[name] = issues
                    _write_cached_issues(store, name, seniority, issues)
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
        "stored": {"repos": len(catalog), "issues": len(flat_issues)},
        "recommendations": recommendations,
    }
