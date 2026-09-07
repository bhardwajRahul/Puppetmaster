"""Local claims retry transient metadata reads without replaying mutations."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from puppetmaster.contracts import ContractConflict
from puppetmaster.identity import StoreIdentityError
from puppetmaster.models import Task, TaskStatus
from puppetmaster.readonly import ReadUnavailable
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore
from puppetmaster.store_contracts import task_binding
from puppetmaster.worker_runtime import WorkerRuntime


class ClaimContentionTests(unittest.TestCase):
    def stores(self):
        for backend in (SwarmStore, SQLiteSwarmStore):
            with TemporaryDirectory() as tmp:
                store = backend(Path(tmp))
                job = store.create_job('claim contention')
                task = Task(job_id=job.id, role='explore', instruction='test', adapter='local')
                store.save_task(task)
                yield store, job, task

    def test_transient_reads_then_claim_exactly_once(self):
        errors = [ReadUnavailable('unable to open database: active reader; sidecars may be missing'),
                  ReadUnavailable('unable to open database: live sidecars; retry after checkpoint')]
        for code in (5, 6, 261, 517, 262):
            error = sqlite3.OperationalError('contended')
            error.sqlite_errorcode = code
            errors.append(error)
        errors.extend(sqlite3.OperationalError(s) for s in
                      ('database is locked', 'database table is locked', 'database schema is locked'))
        for store, job, task in self.stores():
            original = store._claim_job_ref
            failures = list(errors)
            def read(*args, **kwargs):
                if failures:
                    raise failures.pop(0)
                return original(*args, **kwargs)
            with patch.object(store, '_claim_job_ref', side_effect=read):
                claimed = store.claim_task(task.id, 'worker')
            self.assertEqual(claimed.attempts, 1)
            self.assertIsNone(store.claim_task(task.id, 'other'))
            self.assertEqual(len([e for e in store.read_events(job.id) if e['event'] == 'task.claimed']), 1)

    def test_timeout_is_explicit_and_leaves_task_queued(self):
        for store, job, task in self.stores():
            clock = [0.0]
            sleeps = []
            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds
            timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep, time=time.time)
            error = sqlite3.OperationalError('database is locked')
            with patch('puppetmaster.store.time', timer), patch.object(store, '_claim_job_ref', side_effect=error):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    store.claim_task(task.id, 'worker')
            self.assertIs(caught.exception, error)
            self.assertAlmostEqual(clock[0], 5.0)
            self.assertLess(len(sleeps), 100)
            self.assertEqual(store.get_task_by_id(task.id).status, TaskStatus.QUEUED)
            self.assertIsNotNone(store.claim_task(task.id, 'worker'))

    def test_nontransient_errors_are_not_swallowed(self):
        errors = [ReadUnavailable('unable to open database: source changed'),
                  StoreIdentityError('incarnation mismatch'), FileNotFoundError('missing source'),
                  PermissionError('denied'), ValueError('malformed data'), ContractConflict('stale binding'),
                  sqlite3.OperationalError('permission denied'), sqlite3.DatabaseError('malformed')]
        corrupt = sqlite3.OperationalError('database is locked')
        corrupt.sqlite_errorcode = 11
        errors.append(corrupt)
        for store, job, task in self.stores():
            for error in errors:
                with self.subTest(backend=store.backend_name, error=str(error)):
                    with patch.object(store, '_claim_job_ref', side_effect=error) as read:
                        with self.assertRaises(type(error)) as caught:
                            store.claim_task(task.id, 'worker')
                        self.assertIs(caught.exception, error)
                        self.assertEqual(read.call_count, 1)
                    self.assertEqual(store.get_task_by_id(task.id).attempts, 0)

    def test_cancellation_is_rechecked_after_contention(self):
        for store, job, task in self.stores():
            with patch.object(store, 'cancellation_pending', side_effect=[sqlite3.OperationalError('database is locked'), True]) as read:
                self.assertIsNone(store.claim_task(task.id, 'worker'))
                self.assertEqual(read.call_count, 2)
            self.assertEqual(store.get_task_by_id(task.id).attempts, 0)

    def test_cancelled_generation_and_stale_binding_after_retry(self):
        for store, job, task in self.stores():
            ref = store.job_ref(job.id)
            binding = task_binding(task)
            store.request_cancellation(ref, 'stop', [binding])
            with patch.object(store, '_claim_job_ref', side_effect=[sqlite3.OperationalError('database is locked'), ref]):
                self.assertIsNone(store.claim_task(task.id, 'worker'))
            store.reset_subgraph(job.id, [task.id])
            self.assertEqual(store.request_cancellation(ref, 'old', [binding]).outcome, 'stale_binding')
            with patch.object(store, '_claim_job_ref', side_effect=[sqlite3.OperationalError('database is locked'), ref]):
                claimed = store.claim_task(task.id, 'worker')
            self.assertGreater(claimed.generation, task.generation or 0)

    def test_source_fence_after_busy_is_explicit(self):
        for store, job, task in self.stores():
            error = StoreIdentityError('store replaced since selection')
            with patch.object(store, '_claim_job_ref', side_effect=[sqlite3.OperationalError('database is locked'), error]) as read:
                with self.assertRaises(StoreIdentityError) as caught:
                    store.claim_task(task.id, 'worker')
                self.assertIs(caught.exception, error)
                self.assertEqual(read.call_count, 2)
            self.assertEqual(store.get_task_by_id(task.id).attempts, 0)

    def test_local_claim_rejects_missing_replaced_and_changed_identity(self):
        for change in ('missing', 'replaced', 'incarnation'):
            for store, job, task in self.stores():
                with self.subTest(backend=store.backend_name, change=change):
                    path = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
                    if change == 'incarnation':
                        table = 'metadata' if store.backend_name == 'sqlite' else 'projection_meta'
                        with sqlite3.connect(str(path)) as c:
                            c.execute("UPDATE %s SET value=? WHERE key='incarnation'" % table,
                                      ('00000000-0000-0000-0000-000000000001',))
                        c.close()
                    else:
                        saved = path.with_suffix('.saved')
                        path.rename(saved)
                        if change == 'replaced':
                            path.write_bytes(saved.read_bytes())
                    with self.assertRaises(StoreIdentityError):
                        store._claim_job_ref(job.id)
                    if change == 'missing':
                        self.assertFalse(path.exists())

    def test_claim_mutation_errors_are_never_replayed(self):
        for store, job, task in self.stores():
            with patch.object(store, '_atomic_claim', side_effect=sqlite3.OperationalError('database is locked')) as write:
                with self.assertRaises(sqlite3.OperationalError):
                    store.claim_task(task.id, 'worker')
                self.assertEqual(write.call_count, 1)

    def test_short_lease_lock_survives_read_retry(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            job = store.create_job('short lease')
            task = Task(job_id=job.id, role='explore', instruction='test', adapter='local')
            store.save_task(task)
            wall = time.time()
            def busy_sleep(seconds):
                with patch('puppetmaster.store.time.time', return_value=wall + 4):
                    self.assertIsNone(store.claim_task(task.id, 'competitor', lease_seconds=1))
            ref = store.job_ref(job.id)
            with patch.object(store, '_claim_job_ref', side_effect=[sqlite3.OperationalError('database is locked'), ref]), patch('puppetmaster.store.time.sleep', side_effect=busy_sleep):
                claimed = store.claim_task(task.id, 'worker', lease_seconds=1)
            self.assertEqual(claimed.attempts, 1)

    def test_simultaneous_inline_claims_and_completions(self):
        for iteration in range(int(os.environ.get('CLAIM_STRESS_ITERATIONS', '1'))):
            for store, job, task in self.stores():
                for _ in range(3):
                    store.save_task(Task(job_id=job.id, role='explore', instruction='test', adapter='local'))
                barrier = threading.Barrier(4)
                def run(index):
                    barrier.wait(timeout=10)
                    return WorkerRuntime(store, job.id, None, 'worker-%s' % index,
                                         poll_seconds=.01).run_until_idle()
                with ThreadPoolExecutor(4) as pool:
                    results = list(pool.map(run, range(4)))
                self.assertEqual(sum(results), 4, (iteration, store.backend_name))
                tasks = store.list_tasks(job.id)
                self.assertTrue(all(t.status == TaskStatus.COMPLETE and t.attempts == 1 for t in tasks))
                events = [e for e in store.read_events(job.id) if e['event'] == 'task.claimed']
                self.assertEqual(len(events), 4)


if __name__ == '__main__':
    unittest.main()
