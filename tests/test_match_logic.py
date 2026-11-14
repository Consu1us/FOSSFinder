import unittest

from app.catalog import REQUIRED, load_catalog
from app.matching import fallback_rank_repos, profile_terms, shortlist_repos


class CatalogTests(unittest.TestCase):
    def test_catalog_entries_are_complete(self):
        repos = load_catalog()
        self.assertGreaterEqual(len(repos), 40)
        names = [repo["full_name"] for repo in repos]
        self.assertEqual(len(names), len(set(names)))
        for repo in repos:
            for key in REQUIRED:
                self.assertTrue(repo.get(key), repo["full_name"])
            self.assertTrue(repo["html_url"].endswith(repo["full_name"]))
            self.assertIn(repo["difficulty"], {"beginner", "intermediate", "advanced"})


class MatchingTests(unittest.TestCase):
    def test_python_resume_prefers_python_repos(self):
        profile = {
            "languages": ["Python"],
            "frameworks": ["Django"],
            "skills_listed": ["Python", "Django", "PostgreSQL"],
            "skills_inferred": ["REST APIs"],
            "domains": ["web"],
            "seniority": "junior",
        }
        repos = load_catalog()
        shortlist = shortlist_repos(profile, repos)
        names = [repo["full_name"] for repo in shortlist]
        self.assertIn("django/django", names)
        self.assertLess(names.index("django/django"), 5)

    def test_fallback_explains_the_overlap(self):
        profile = {
            "languages": ["Go"],
            "frameworks": [],
            "skills_listed": ["Go"],
            "skills_inferred": [],
            "domains": [],
            "seniority": "mid",
        }
        shortlist = shortlist_repos(profile, load_catalog(), limit=3)
        ranked = fallback_rank_repos(profile, shortlist, limit=1)
        self.assertIn("go", ranked[0]["why"].lower())
        self.assertGreater(ranked[0]["score"], 0)

    def test_profile_terms_keep_unique_tokens(self):
        terms = profile_terms(
            {"languages": ["Python"], "skills_listed": ["python", "FastAPI"], "frameworks": []}
        )
        self.assertEqual(terms.count("python"), 1)
        self.assertIn("fastapi", terms)


if __name__ == "__main__":
    unittest.main()
