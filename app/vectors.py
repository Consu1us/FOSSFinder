import hashlib
import logging

from google import genai
from google.genai import types

from app.matching import _overlap, _score, profile_terms, shortlist_repos

logger = logging.getLogger("fossfinder.vectors")

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
DEFAULT_VECTOR_INDEX = "repo_embeddings"
EMBED_BATCH = 32
SHORTLIST_LIMIT = 12


def vector_index_definition(dimensions: int = EMBEDDING_DIMENSIONS) -> dict:
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": dimensions,
                "similarity": "cosine",
            },
            {"type": "filter", "path": "reviewed"},
            {"type": "filter", "path": "source"},
        ]
    }


def repo_embedding_text(repo: dict) -> str:
    languages = ", ".join(str(item) for item in repo.get("languages") or [])
    skills = ", ".join(str(item) for item in repo.get("skills") or [])
    domains = ", ".join(str(item) for item in repo.get("domains") or [])
    lines = [
        str(repo.get("full_name") or "").strip(),
        str(repo.get("description") or "").strip(),
        f"Languages: {languages}" if languages else "",
        f"Skills: {skills}" if skills else "",
        f"Domains: {domains}" if domains else "",
        str(repo.get("contribution_notes") or "").strip(),
    ]
    return "\n".join(line for line in lines if line)


def profile_embedding_text(profile: dict) -> str:
    lines = []
    summary = str(profile.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    for label, key in (
        ("Listed skills", "skills_listed"),
        ("Inferred skills", "skills_inferred"),
        ("Languages", "languages"),
        ("Frameworks", "frameworks"),
        ("Domains", "domains"),
    ):
        values = [str(item).strip() for item in profile.get(key) or [] if str(item).strip()]
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    seniority = str(profile.get("seniority") or "").strip()
    if seniority:
        lines.append(f"Seniority: {seniority}")
    return "\n".join(lines) or "software developer"


def embedding_hash(text: str, model: str, dimensions: int) -> str:
    payload = f"{model}\n{dimensions}\n{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _valid_vector(vector, dimensions: int) -> bool:
    return isinstance(vector, list) and len(vector) == dimensions and all(
        isinstance(item, (int, float)) for item in vector
    )


def vectors_from_response(response, dimensions: int) -> list[list[float]]:
    embeddings = getattr(response, "embeddings", None) or []
    vectors = []
    for item in embeddings:
        values = getattr(item, "values", None)
        if values is None and isinstance(item, dict):
            values = item.get("values")
        if not values:
            vectors.append([])
            continue
        vectors.append([float(value) for value in values])
    return vectors


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str, dimensions: int = EMBEDDING_DIMENSIONS, client=None):
        self._model = model
        self._dimensions = dimensions
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=20_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = self._client.models.embed_content(
            model=self._model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=self._dimensions),
        )
        vectors = vectors_from_response(response, self._dimensions)
        if len(vectors) < len(texts):
            vectors.extend([] for _ in range(len(texts) - len(vectors)))
        return vectors[: len(texts)]


def plan_embeddings(repos: list[dict], records: dict[str, dict], model: str, dimensions: int):
    pending = []
    skipped = 0
    for repo in repos:
        full_name = str(repo.get("full_name") or "").strip()
        if not full_name:
            continue
        text = repo_embedding_text(repo)
        digest = embedding_hash(text, model, dimensions)
        current = records.get(full_name) or {}
        if (
            current.get("embedding_hash") == digest
            and current.get("embedding_model") == model
            and current.get("embedding_dimensions") == dimensions
            and current.get("has_embedding")
        ):
            skipped += 1
            continue
        pending.append({"full_name": full_name, "text": text, "embedding_hash": digest})
    return pending, skipped


def embed_pending(pending: list[dict], embedder: GeminiEmbedder, model: str) -> tuple[list[dict], int]:
    ready = []
    failed = 0
    for batch in _chunks(pending, EMBED_BATCH):
        try:
            vectors = embedder.embed_texts([item["text"] for item in batch])
        except Exception as exc:
            failed += len(batch)
            logger.warning(
                "Embedding batch failed (%s). %s catalog repos were left unchanged.",
                exc.__class__.__name__,
                len(batch),
            )
            continue
        for item, vector in zip(batch, vectors):
            if not _valid_vector(vector, embedder._dimensions):
                failed += 1
                continue
            ready.append(
                {
                    "full_name": item["full_name"],
                    "embedding": vector,
                    "embedding_hash": item["embedding_hash"],
                    "embedding_model": model,
                    "embedding_dimensions": embedder._dimensions,
                }
            )
    return ready, failed


def update_vector_search(store, settings, embedder=None, repos: list[dict] | None = None) -> dict:
    """Embed changed catalog rows and make sure the Atlas vector index exists.

    Resume text and profile embeddings are never written. A failure here is logged
    and reported; keyword shortlisting still works.
    """
    model = settings.gemini_embedding_model
    index_name = settings.vector_index_name
    status = {
        "status": "ok",
        "index": index_name,
        "index_ready": False,
        "embedded_count": 0,
        "embedded_this_startup": 0,
        "skipped": 0,
        "failed": 0,
        "model": model,
        "dimensions": EMBEDDING_DIMENSIONS,
    }
    try:
        catalog = repos if repos is not None else []
        if repos is None:
            from app.catalog import load_catalog

            catalog = load_catalog()
        records = store.embedding_records()
        pending, skipped = plan_embeddings(catalog, records, model, EMBEDDING_DIMENSIONS)
        status["skipped"] = skipped
        if pending and settings.gemini_api_key:
            embedder = embedder or GeminiEmbedder(settings.gemini_api_key, model)
            ready, failed = embed_pending(pending, embedder, model)
            status["failed"] = failed
            if ready:
                store.save_embeddings(ready)
                status["embedded_this_startup"] = len(ready)
        elif pending:
            logger.info("GEMINI_API_KEY is unset, so catalog embeddings were not created.")
        try:
            index = store.ensure_vector_index(index_name, vector_index_definition())
            status["index_ready"] = bool(index.get("index_ready"))
            status["status"] = str(index.get("status") or "ok")
        except Exception as exc:
            status["status"] = f"error: {exc.__class__.__name__}"
            status["index_ready"] = False
            logger.warning("Atlas vector index was not created: %s", exc.__class__.__name__)
        try:
            status["embedded_count"] = store.embedded_repo_count()
        except Exception:
            status["embedded_count"] = status["embedded_this_startup"]
    except Exception as exc:
        status["status"] = f"error: {exc.__class__.__name__}"
        logger.warning("Vector search setup skipped: %s", exc.__class__.__name__)
    if status["embedded_this_startup"] or status["skipped"]:
        logger.info(
            "Catalog embeddings: %s written, %s unchanged, %s failed. Index %s is %s.",
            status["embedded_this_startup"],
            status["skipped"],
            status["failed"],
            index_name,
            "ready" if status["index_ready"] else status["status"],
        )
    return status


def choose_shortlist(profile: dict, catalog: list[dict], store, settings, embedder=None):
    """Return (repos, method). method is hybrid or keyword. The query vector is not stored."""
    try:
        if not settings.gemini_api_key:
            raise RuntimeError("no embedding key")
        embedder = embedder or GeminiEmbedder(settings.gemini_api_key, settings.gemini_embedding_model)
        vector = embedder.embed_texts([profile_embedding_text(profile)])[0]
        if not _valid_vector(vector, EMBEDDING_DIMENSIONS):
            raise ValueError("query embedding had the wrong size")
        hits = store.vector_candidates(vector, settings.vector_index_name, limit=SHORTLIST_LIMIT)
        blended = _blend(profile, catalog, hits, SHORTLIST_LIMIT)
        if not blended:
            raise ValueError("vector search returned no catalog rows")
        return blended, "hybrid"
    except Exception as exc:
        logger.warning("Vector shortlist unavailable (%s). Using keyword overlap.", exc.__class__.__name__)
        return shortlist_repos(profile, catalog, limit=SHORTLIST_LIMIT), "keyword"


def _blend(profile: dict, catalog: list[dict], hits: list[dict], limit: int) -> list[dict]:
    by_name = {str(repo.get("full_name") or "").lower(): repo for repo in catalog}
    terms = profile_terms(profile)
    seniority = {
        "student": 0,
        "junior": 0,
        "unspecified": 1,
        "mid": 1,
        "senior": 2,
        "staff": 2,
        "principal": 2,
    }.get(str(profile.get("seniority") or "").lower(), 1)
    blended = []
    seen: set[str] = set()
    for hit in hits:
        name = str(hit.get("full_name") or "")
        key = name.lower()
        repo = by_name.get(key)
        if not repo or key in seen:
            continue
        seen.add(key)
        overlap = _overlap(terms, repo)
        keyword = _score(overlap, repo, seniority) if overlap else 0
        try:
            vector_score = float(hit.get("score") or 0)
        except (TypeError, ValueError):
            vector_score = 0.0
        vector_score = max(0.0, min(vector_score, 1.0))
        copy = dict(repo)
        copy["overlap"] = overlap
        copy["vector_score"] = vector_score
        copy["local_score"] = int(round(0.7 * vector_score * 100 + 0.3 * keyword))
        blended.append(copy)
    blended.sort(key=lambda item: item["local_score"], reverse=True)
    return blended[:limit]
