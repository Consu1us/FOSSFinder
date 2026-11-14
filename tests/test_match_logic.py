import unittest

from app.catalog import REQUIRED, load_catalog
from app.config import site_access
from app.matching import fallback_rank_repos, profile_terms, shortlist_repos
from app.scoring import fallback_rank


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


class ScoringTests(unittest.TestCase):
    def test_language_match_ranks_higher(self):
        profile = {"languages": ["Python"], "frameworks": ["FastAPI"], "domains": []}
        issues = [
            {
                "html_url": "https://github.com/a/b/issues/1",
                "title": "Fix docs",
                "body": "",
                "labels": [],
                "updated_at": None,
                "repo": {"language": "Ruby", "description": "", "stars": 10},
            },
            {
                "html_url": "https://github.com/a/c/issues/2",
                "title": "Add FastAPI example",
                "body": "help with fastapi",
                "labels": ["good first issue"],
                "updated_at": None,
                "repo": {"language": "Python", "description": "api", "stars": 100},
            },
        ]
        ranked = fallback_rank(profile, issues)
        self.assertEqual(ranked[0]["issue_url"], issues[1]["html_url"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


class SiteAccessTests(unittest.TestCase):
    def test_fossfinder_work_allows_www_and_localhost(self):
        public, hosts, origins = site_access("https://fossfinder.work")
        self.assertEqual(public, "https://fossfinder.work")
        self.assertIn("www.fossfinder.work", hosts)
        self.assertIn("127.0.0.1", hosts)
        self.assertIn("https://fossfinder.work", origins)
        self.assertIn("https://www.fossfinder.work", origins)


if __name__ == "__main__":
    unittest.main()
