import unittest

from app.catalog import REQUIRED, load_catalog
from app.config import site_access
from app.matching import fallback_rank_repos, profile_terms, shortlist_repos


class CatalogTests(unittest.TestCase):
    def test_catalog_entries_are_complete(self):
        repos = load_catalog()
        self.assertEqual(len(repos), 103)
        names = [repo["full_name"] for repo in repos]
        self.assertEqual(len(names), len(set(names)))
        for repo in repos:
            for key in REQUIRED:
                self.assertTrue(repo.get(key), repo["full_name"])
            self.assertTrue(repo["html_url"].endswith(repo["full_name"]))
            self.assertIn(repo["difficulty"], {"beginner", "intermediate", "advanced"})
            self.assertIn(
                repo["contribution_workflow"],
                {"github-pr", "mailing-list", "external"},
            )

    def test_linux_and_openfoam_do_not_take_github_pull_requests(self):
        repos = {repo["full_name"]: repo for repo in load_catalog()}
        linux = repos["torvalds/linux"]
        foam = repos["OpenFOAM/OpenFOAM-dev"]
        self.assertEqual(linux["difficulty"], "advanced")
        self.assertEqual(linux["contribution_workflow"], "mailing-list")
        self.assertEqual(
            linux["contributing_url"],
            "https://docs.kernel.org/process/submitting-patches.html",
        )
        self.assertIn("mailing", linux["contribution_notes"].lower())
        self.assertEqual(foam["difficulty"], "advanced")
        self.assertEqual(foam["contribution_workflow"], "external")
        self.assertEqual(foam["contributing_url"], "https://openfoam.org/dev/how-to-contribute/")
        self.assertIn("does not take github pull requests", foam["contribution_notes"].lower())

    def test_added_repos_take_github_pull_requests(self):
        repos = {repo["full_name"]: repo for repo in load_catalog()}
        for name in ADDED_REPOS:
            repo = repos[name]
            self.assertEqual(repo["contribution_workflow"], "github-pr", name)
            self.assertEqual(repo["contributing_url"], "", name)


ADDED_REPOS = (
    "AntennaPod/AntennaPod",
    "thunderbird/thunderbird-android",
    "wordpress-mobile/WordPress-iOS",
    "obsproject/obs-studio",
    "neovim/neovim",
    "duckdb/duckdb",
    "qgis/QGIS",
    "zephyrproject-rtos/zephyr",
    "jellyfin/jellyfin",
    "PowerShell/PowerShell",
    "bevyengine/bevy",
    "zed-industries/zed",
    "servo/servo",
    "scipy/scipy",
    "tidyverse/ggplot2",
    "JuliaLang/julia",
    "zaproxy/zaproxy",
    "keycloak/keycloak",
    "home-assistant/core",
    "nextcloud/server",
    "mastodon/mastodon",
    "WordPress/gutenberg",
    "excalidraw/excalidraw",
    "openstreetmap/iD",
    "ollama/ollama",
)


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

    def test_scientific_python_shortlist_leads_with_scipy(self):
        profile = {
            "languages": ["Python"],
            "frameworks": ["NumPy", "SciPy"],
            "skills_listed": ["Python", "NumPy", "SciPy"],
            "skills_inferred": [],
            "domains": ["scientific"],
            "seniority": "mid",
        }
        shortlist = shortlist_repos(profile, load_catalog())
        names = [repo["full_name"] for repo in shortlist]
        self.assertEqual(len(names), 12)
        self.assertEqual(names[0], "scipy/scipy")
        self.assertIn("numpy/numpy", names)

    def test_android_resume_reaches_the_new_mobile_apps(self):
        profile = {
            "languages": ["Kotlin", "Java"],
            "frameworks": ["Android"],
            "skills_listed": ["Kotlin", "Android", "Java"],
            "skills_inferred": [],
            "domains": ["mobile"],
            "seniority": "junior",
        }
        names = [repo["full_name"] for repo in shortlist_repos(profile, load_catalog())]
        self.assertIn("AntennaPod/AntennaPod", names)
        self.assertIn("thunderbird/thunderbird-android", names)
        self.assertIn("wordpress-mobile/WordPress-iOS", names)


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
