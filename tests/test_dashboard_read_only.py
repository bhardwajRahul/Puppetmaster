"""Dashboard polls never take the SQLite writer lock or mutate job state."""
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from puppetmaster.dashboard import build_job_snapshot, list_jobs_snapshot, viewer_store
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store_factory import create_store


class DashboardReadOnlyTests(unittest.TestCase):
    def test_polls_do_not_write(self):
        with TemporaryDirectory() as root:
            supervisor = create_store("sqlite", root, mode="ensure")
            job = supervisor.create_job("audit the thing")
            with patch.object(SQLiteSwarmStore, "ensure_schema", side_effect=AssertionError("ensure_schema")), \
                 patch.object(SQLiteSwarmStore, "_writer_scope", side_effect=AssertionError("writer lock")):
                store = viewer_store("sqlite", root)
                self.assertEqual([j["id"] for j in list_jobs_snapshot(store)], [job.id])
                snapshot = build_job_snapshot(viewer_store("sqlite", root), job.id)
            self.assertEqual(snapshot["job"]["id"], job.id)
            self.assertIn("frontier", snapshot)
            self.assertIn("totals", snapshot["budget"])

    def test_new_project_without_schema_still_opens(self):
        with TemporaryDirectory() as root:
            store = viewer_store("sqlite", root)
            self.assertEqual(store.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()


class DashboardIndexCostTests(unittest.TestCase):
    def test_index_decodes_only_the_rows_it_shows(self):
        from puppetmaster import sqlite_store
        with TemporaryDirectory() as root:
            supervisor = create_store("sqlite", root, mode="ensure")
            ids = [supervisor.create_job(f"goal {i}").id for i in range(12)]
            decoded = []
            real = sqlite_store.job_from_dict
            with patch.object(sqlite_store, "job_from_dict", side_effect=lambda d: decoded.append(1) or real(d)):
                rows = list_jobs_snapshot(viewer_store("sqlite", root), limit=5)
            self.assertEqual(len(rows), 5)
            self.assertEqual(len(decoded), 5)
            self.assertEqual({r["id"] for r in rows} <= set(ids), True)
