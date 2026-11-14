import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from pymongo import UpdateOne

from app.store import AtlasStore, cache_is_fresh


class CacheFreshnessTests(unittest.TestCase):
    def test_recent_timestamp_is_fresh(self):
        now = datetime(2026, 11, 14, tzinfo=timezone.utc)
        self.assertTrue(cache_is_fresh(now, now))
        self.assertTrue(cache_is_fresh(now - timedelta(hours=1), now))

    def test_timestamp_older_than_an_hour_is_stale(self):
        now = datetime(2026, 11, 14, tzinfo=timezone.utc)
        self.assertFalse(cache_is_fresh(now - timedelta(hours=1, seconds=1), now))

    def test_naive_timestamp_is_treated_as_utc(self):
        now = datetime(2026, 11, 14, tzinfo=timezone.utc)
        self.assertTrue(cache_is_fresh(datetime(2026, 11, 14), now))


class CatalogWriteTests(unittest.TestCase):
    def test_ensure_catalog_uses_one_bulk_write(self):
        store = AtlasStore.__new__(AtlasStore)
        store.repos = MagicMock()
        count = store.ensure_catalog(
            [
                {"full_name": "a/b", "description": "x"},
                {"full_name": "", "description": "skip"},
                {"full_name": "c/d", "description": None},
            ]
        )
        self.assertEqual(count, 2)
        store.repos.bulk_write.assert_called_once()
        self.assertFalse(store.repos.bulk_write.call_args.kwargs["ordered"])
        operations = store.repos.bulk_write.call_args.args[0]
        self.assertEqual(len(operations), 2)
        self.assertIsInstance(operations[0], UpdateOne)
        self.assertEqual(operations[0]._filter, {"full_name": "a/b"})
        self.assertTrue(operations[0]._upsert)
        self.assertEqual(operations[0]._doc["$set"]["reviewed"], True)
        self.assertEqual(operations[0]._doc["$set"]["source"], "catalog")
        self.assertEqual(operations[0]._doc["$set"]["description"], "x")
        self.assertEqual(operations[1]._filter, {"full_name": "c/d"})
        self.assertNotIn("description", operations[1]._doc["$set"])

    def test_ensure_catalog_skips_the_round_trip_when_nothing_is_loadable(self):
        store = AtlasStore.__new__(AtlasStore)
        store.repos = MagicMock()
        self.assertEqual(store.ensure_catalog([{"full_name": ""}]), 0)
        store.repos.bulk_write.assert_not_called()


class StarterIssueCacheTests(unittest.TestCase):
    def _store(self) -> AtlasStore:
        store = AtlasStore.__new__(AtlasStore)
        store.issue_cache = MagicMock()
        return store

    def test_cache_write_normalizes_seniority(self):
        store = self._store()
        store.cache_starter_issues("django/django", "Junior", [{"title": "Fix docs"}])
        args, kwargs = store.issue_cache.update_one.call_args
        self.assertEqual(args[0], {"repo_full_name": "django/django", "seniority": "junior"})
        self.assertEqual(kwargs["upsert"], True)
        stored = args[1]["$set"]
        self.assertEqual(stored["issues"], [{"title": "Fix docs"}])
        self.assertIsInstance(stored["cached_at"], datetime)

    def test_fresh_empty_list_is_a_hit(self):
        store = self._store()
        store.issue_cache.find_one.return_value = {
            "issues": [],
            "cached_at": datetime.now(timezone.utc),
        }
        self.assertEqual(store.cached_starter_issues("a/b", "junior"), [])

    def test_stale_or_missing_documents_are_misses(self):
        store = self._store()
        store.issue_cache.find_one.return_value = {
            "issues": [{"title": "old"}],
            "cached_at": datetime.now(timezone.utc) - timedelta(hours=2),
        }
        self.assertIsNone(store.cached_starter_issues("a/b", "junior"))
        store.issue_cache.find_one.return_value = None
        self.assertIsNone(store.cached_starter_issues("a/b", "junior"))


class ContributingGuideCacheTests(unittest.TestCase):
    def test_empty_url_is_a_fresh_hit(self):
        store = AtlasStore.__new__(AtlasStore)
        store.guides = MagicMock()
        store.guides.find_one.return_value = {
            "url": "",
            "cached_at": datetime.now(timezone.utc),
        }
        self.assertEqual(store.cached_contributing_guide("torvalds/linux"), "")

    def test_cache_write_stores_the_url(self):
        store = AtlasStore.__new__(AtlasStore)
        store.guides = MagicMock()
        store.cache_contributing_guide(
            "torvalds/linux",
            "https://docs.kernel.org/process/submitting-patches.html",
        )
        stored = store.guides.update_one.call_args.args[1]["$set"]
        self.assertEqual(
            stored["url"],
            "https://docs.kernel.org/process/submitting-patches.html",
        )
        self.assertEqual(store.guides.update_one.call_args.kwargs["upsert"], True)


class MatchRecordTests(unittest.TestCase):
    def test_record_match_keeps_repo_names_only(self):
        store = AtlasStore.__new__(AtlasStore)
        store.matches = MagicMock()
        inserted = MagicMock()
        inserted.inserted_id = "abc123"
        store.matches.insert_one.return_value = inserted
        match_id = store.record_match(["django/django", "django/django", " ", "pallets/flask"])
        self.assertEqual(match_id, "abc123")
        document = store.matches.insert_one.call_args.args[0]
        self.assertEqual(document["repos"], ["django/django", "pallets/flask"])
        self.assertEqual(document["recommendation_count"], 2)
        self.assertNotIn("profile", document)
        self.assertNotIn("rankings", document)
        self.assertNotIn("why", document)
        self.assertFalse(hasattr(AtlasStore, "save_match"))


class IndexSetupTests(unittest.TestCase):
    def test_issue_cache_ttl_index_is_one_hour(self):
        with patch("app.store.MongoClient") as client_cls:
            client = client_cls.return_value
            database = MagicMock()
            client.__getitem__.return_value = database
            collections: dict[str, MagicMock] = {}

            def get_col(name):
                collections.setdefault(name, MagicMock())
                return collections[name]

            database.__getitem__.side_effect = get_col
            AtlasStore("mongodb://example", "fossfinder")
            self.assertTrue(client_cls.call_args.kwargs["tz_aware"])
            self.assertEqual(client_cls.call_args.kwargs["serverSelectionTimeoutMS"], 8000)
            ttl = [
                call.kwargs.get("expireAfterSeconds")
                for call in collections["starter_issue_cache"].create_index.call_args_list
            ]
            self.assertIn(3600, ttl)
            self.assertTrue(collections["repos"].create_index.called)
            self.assertFalse(hasattr(AtlasStore, "upsert_repos"))
            self.assertFalse(hasattr(AtlasStore, "recent_repos"))
