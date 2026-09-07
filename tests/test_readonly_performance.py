"""Bound helper startup and retry work independently of machine speed."""
import gc
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from readonly_fixtures import damaged_sidecars

from puppetmaster import readonly
from puppetmaster.identity import StoreIdentityError
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class ReadonlyPerformanceTests(unittest.TestCase):
    def test_many_metadata_reads_share_one_helper_and_release_locks(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=cls.backend_name), TemporaryDirectory() as root:
                store = cls(root)
                job = store.create_job('bounded reads')
                with patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn, \
                        patch.object(readonly, 'selection', wraps=readonly.selection) as fences:
                    started = time.monotonic()
                    for _ in range(100):
                        ref = store.job_ref(job.id)
                        page = store.list_job_summaries(limit=1, max_scan=1)
                        self.assertEqual(len(page.items), 1)
                        self.assertEqual(page.items[0].job_ref.job_id, ref.job_id)
                    elapsed = time.monotonic() - started
                    self.assertEqual(spawn.call_count, 1)
                    # Fixed work per session, with no repeated process startup
                    # or source scan. Elapsed time is diagnostic, not a flaky gate.
                    self.assertLessEqual(fences.call_count, 1000, elapsed)
                transport = store._readonly_transport
                path = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
                with closing(sqlite3.connect(path, timeout=0)) as writer:
                    writer.execute('BEGIN EXCLUSIVE')
                    writer.rollback()
                fences.reset_mock()  # Recorded call arguments also own the store.
                del store
                gc.collect()
                self.assertIsNotNone(transport.process.poll())
                self.assertTrue(transport.process.stdin.closed)
                self.assertTrue(transport.process.stdout.closed)

    def test_live_source_retry_work_is_bounded_by_virtual_elapsed_time(self):
        with TemporaryDirectory() as root:
            store = SwarmStore(root)
            clock = [0.0]
            def sleep(seconds):
                clock[0] += seconds
            # Helper finalizers also sleep while reaping subprocesses.
            # Keep their real clock separate from the retry budget under test.
            timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
            with patch.object(readonly, 'ReadConnection', side_effect=readonly.ReadUnavailable('live sidecars')) as opens, \
                    patch.object(readonly, 'time', timer):
                for _ in range(100):
                    with self.assertRaises(readonly.ReadUnavailable):
                        readonly.connect(store, reuse=True)
                self.assertLessEqual(opens.call_count, 400)
                self.assertLessEqual(clock[0], 10.01)

    def test_cross_project_lookup_reuses_one_helper_and_caches_proven_membership(self):
        from puppetmaster import state
        with TemporaryDirectory() as tmp:
            stores = [SQLiteSwarmStore(Path(tmp) / str(index)) for index in range(16)]
            jobs = [store.create_job('lookup') for store in stores]
            roots = [store.root for store in stores]
            with patch.object(state, 'list_project_state_dirs', return_value=roots), \
                    patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn:
                for _ in range(100):
                    self.assertEqual(state.find_state_dir_for_job(jobs[0].id), roots[0])
                self.assertEqual(spawn.call_count, 1)
                # Adding a duplicate must invalidate the cached negative.
                with stores[1]._writer_scope() as c:
                    c.execute("INSERT INTO jobs(id,data) VALUES(?, '{}')", (jobs[0].id,))
                with self.assertRaisesRegex(ValueError, 'ambiguous'):
                    state.find_state_dir_for_job(jobs[0].id)
                self.assertEqual(spawn.call_count, 2)

    def test_ownership_cache_cannot_hide_wal_commits_with_missing_sidecars(self):
        from puppetmaster import state
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(tmp)
            store.create_job('cached')
            with patch.object(state, 'list_project_state_dirs', return_value=[store.root]):
                self.assertIsNone(state.find_state_dir_for_job('new_job'))
                with closing(sqlite3.connect(store.db_path)) as writer:
                    writer.execute("INSERT INTO jobs(id,data) VALUES('new_job','{}')")
                    writer.commit()
                    sidecars = [Path(str(store.db_path) + suffix) for suffix in ('-wal', '-shm')]
                    with damaged_sidecars(writer, store.db_path):
                        with self.assertRaises(readonly.ReadUnavailable):
                            state.resolve_job_state(job_id='new_job', default_dir=store.root)
                        self.assertIsNone(state.find_state_dir_for_job('new_job'))
                        self.assertTrue(all(not path.exists() for path in sidecars))
                self.assertEqual(state.find_state_dir_for_job('new_job'), store.root)

    def test_missing_source_identity_does_not_retry_contention(self):
        with TemporaryDirectory() as root:
            store = SwarmStore(Path(root) / 'missing')
            with patch.object(readonly, 'ReadConnection', side_effect=readonly.ReadUnavailable(
                    'unable to open database: source missing')) as opens, \
                    patch.object(readonly.time, 'sleep', side_effect=AssertionError('missing source retried')):
                with self.assertRaises(readonly.ReadUnavailable):
                    _ = store.incarnation
                self.assertEqual(opens.call_count, 1)

    def test_cached_helper_refreshes_commits_and_fences_replacement(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=cls.backend_name), TemporaryDirectory() as tmp:
                root = Path(tmp) / 'state'
                store = cls(root)
                first = store.create_job('first')
                ref = store.job_ref(first.id)
                process = store._readonly_transport.process
                store.create_job('second')
                self.assertEqual(len(store.list_job_summaries().items), 2)
                self.assertIs(store._readonly_transport.process, process)
                root.rename(Path(tmp) / 'old')
                replacement = cls(root)
                replacement.create_job('replacement')
                with self.assertRaises(StoreIdentityError):
                    store.validate_job_ref(ref)
                self.assertNotEqual(replacement.incarnation, ref.incarnation)


if __name__ == '__main__':
    unittest.main()
