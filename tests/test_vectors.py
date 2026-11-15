import unittest
from unittest.mock import MagicMock, patch

from pymongo.operations import SearchIndexModel

from app.store import AtlasStore
from app.vectors import (
    EMBEDDING_DIMENSIONS,
    choose_shortlist,
    embed_pending,
    embedding_hash,
    plan_embeddings,
    profile_embedding_text,
    repo_embedding_text,
    vector_index_definition,
)


def _repo(full_name, **extra):
    repo = {
        "full_name": full_name,
        "description": extra.pop("description", "A library"),
        "languages": extra.pop("languages", ["Python"]),
        "skills": extra.pop("skills", ["python"]),
        "domains": extra.pop("domains", ["data"]),
        "difficulty": extra.pop("difficulty", "intermediate"),
        "contribution_notes": extra.pop("contribution_notes", "Pull requests are welcome."),
    }
    repo.update(extra)
    return repo


def _profile():
    return {
        "summary": "Builds scientific Python tools",
        "skills_listed": ["Python", "SciPy"],
        "skills_inferred": ["NumPy"],
        "languages": ["Python"],
        "frameworks": [],
        "domains": ["scientific"],
        "seniority": "mid",
    }


class _Settings:
    gemini_api_key = "test-key"
    gemini_embedding_model = "gemini-embedding-001"
    vector_index_name = "repo_embeddings"


class EmbeddingPlanTests(unittest.TestCase):
    def test_hash_changes_when_the_catalog_text_or_model_changes(self):
        text = repo_embedding_text(_repo("scipy/scipy", description="scientific"))
        self.assertIn("scientific", text)
        self.assertIn("scipy/scipy", text)
        same = embedding_hash(text, "gemini-embedding-001", 768)
        self.assertEqual(same, embedding_hash(text, "gemini-embedding-001", 768))
        self.assertNotEqual(same, embedding_hash(text + " more", "gemini-embedding-001", 768))
        self.assertNotEqual(same, embedding_hash(text, "other-model", 768))

    def test_unchanged_repos_are_not_embedded_again(self):
        repo = _repo("scipy/scipy")
        digest = embedding_hash(repo_embedding_text(repo), "gemini-embedding-001", 768)
        records = {
            "scipy/scipy": {
                "embedding_hash": digest,
                "embedding_model": "gemini-embedding-001",
                "embedding_dimensions": 768,
                "has_embedding": True,
            }
        }
        pending, skipped = plan_embeddings([repo], records, "gemini-embedding-001", 768)
        self.assertEqual(pending, [])
        self.assertEqual(skipped, 1)

    def test_profile_text_includes_skills_and_summary_only(self):
        text = profile_embedding_text(_profile())
        self.assertIn("Builds scientific Python tools", text)
        self.assertIn("Python", text)
        self.assertIn("NumPy", text)
        self.assertNotIn("resume", text.lower())


class EmbedBatchTests(unittest.TestCase):
    def test_a_failed_batch_does_not_stop_the_next_batch(self):
        pending = [
            {"full_name": "a/b", "text": "bad", "embedding_hash": "1"},
            {"full_name": "c/d", "text": "good", "embedding_hash": "2"},
        ]

        class Embedder:
            def __init__(self):
                self._dimensions = EMBEDDING_DIMENSIONS
                self.calls = 0

            def embed_texts(self, texts):
                self.calls += 1
                if texts == ["bad"]:
                    raise RuntimeError("overloaded")
                return [[0.2] * EMBEDDING_DIMENSIONS]

        embedder = Embedder()
        with patch("app.vectors.EMBED_BATCH", 1):
            ready, failed = embed_pending(pending, embedder, "gemini-embedding-001")
        self.assertEqual(failed, 1)
        self.assertEqual([row["full_name"] for row in ready], ["c/d"])
        self.assertEqual(embedder.calls, 2)


class ShortlistTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            _repo(
                "scipy/scipy",
                description="scientific python",
                skills=["python", "scipy"],
                domains=["scientific"],
                difficulty="advanced",
            ),
            _repo(
                "django/django",
                description="web framework",
                skills=["python", "django"],
                domains=["web"],
            ),
        ]
        self.profile = _profile()

    def test_vector_failure_uses_keyword_overlap(self):
        class Embedder:
            _dimensions = EMBEDDING_DIMENSIONS

            def embed_texts(self, texts):
                raise RuntimeError("embeddings down")

        store = MagicMock()
        shortlist, method = choose_shortlist(
            self.profile, self.catalog, store, _Settings(), embedder=Embedder()
        )
        self.assertEqual(method, "keyword")
        self.assertIn("scipy/scipy", [repo["full_name"] for repo in shortlist])
        store.vector_candidates.assert_not_called()
        store.save_embeddings.assert_not_called()

    def test_hybrid_shortlist_blends_vector_and_keyword_scores(self):
        seen = {}

        class Embedder:
            _dimensions = EMBEDDING_DIMENSIONS

            def embed_texts(self, texts):
                seen["text"] = texts[0]
                return [[0.1] * EMBEDDING_DIMENSIONS]

        store = MagicMock()
        store.vector_candidates.return_value = [
            {"full_name": "django/django", "score": 0.4},
            {"full_name": "scipy/scipy", "score": 0.95},
            {"full_name": "missing/repo", "score": 0.99},
        ]
        shortlist, method = choose_shortlist(
            self.profile, self.catalog, store, _Settings(), embedder=Embedder()
        )
        self.assertEqual(method, "hybrid")
        self.assertEqual(shortlist[0]["full_name"], "scipy/scipy")
        self.assertLessEqual(len(shortlist), 12)
        self.assertIn("SciPy", seen["text"])
        self.assertNotIn("profile", store.save_embeddings.call_args_list)
        store.save_embeddings.assert_not_called()
        store.vector_candidates.assert_called_once()
        query = store.vector_candidates.call_args.args[0]
        self.assertEqual(len(query), EMBEDDING_DIMENSIONS)


class VectorIndexTests(unittest.TestCase):
    def _store(self):
        store = AtlasStore.__new__(AtlasStore)
        store.repos = MagicMock()
        return store

    def test_index_is_created_when_missing(self):
        store = self._store()
        store.repos.list_search_indexes.return_value = []
        state = store.ensure_vector_index("repo_embeddings", vector_index_definition())
        model = store.repos.create_search_index.call_args.args[0]
        self.assertIsInstance(model, SearchIndexModel)
        self.assertEqual(model.document["name"], "repo_embeddings")
        self.assertEqual(model.document["type"], "vectorSearch")
        definition = model.document["definition"]
        self.assertEqual(definition["fields"][0]["similarity"], "cosine")
        self.assertEqual(definition["fields"][0]["numDimensions"], 768)
        self.assertEqual(definition["fields"][0]["path"], "embedding")
        self.assertEqual(state["status"], "PENDING")

    def test_existing_queryable_index_is_left_alone(self):
        store = self._store()
        store.repos.list_search_indexes.return_value = [
            {"name": "repo_embeddings", "status": "READY", "queryable": True}
        ]
        state = store.ensure_vector_index("repo_embeddings", vector_index_definition())
        store.repos.create_search_index.assert_not_called()
        self.assertTrue(state["index_ready"])

    def test_vector_query_requests_the_top_12(self):
        store = self._store()
        store.repos.aggregate.return_value = []
        store.vector_candidates([0.2] * EMBEDDING_DIMENSIONS, "repo_embeddings", limit=12)
        pipeline = store.repos.aggregate.call_args.args[0]
        search = pipeline[0]["$vectorSearch"]
        self.assertEqual(search["index"], "repo_embeddings")
        self.assertEqual(search["limit"], 12)
        self.assertEqual(search["path"], "embedding")
        self.assertEqual(search["filter"], {"reviewed": True, "source": "catalog"})
        self.assertNotIn("embedding", pipeline[1]["$project"])
        self.assertEqual(pipeline[1]["$project"]["full_name"], 1)
