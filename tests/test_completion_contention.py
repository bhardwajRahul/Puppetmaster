"""Completion intent contention must not lose accepted worker output."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from puppetmaster.contracts import ContractConflict
from puppetmaster.models import AgentRun, Task, TaskStatus, now_iso
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class CompletionContentionTests(unittest.TestCase):
    def make_task(self, store, job):
        task = Task(job_id=job.id, role='explore', instruction='complete')
        store.save_task(task)
        task = store.claim_task(task.id, 'worker')
        run = AgentRun(job_id=job.id, task_id=task.id, role=task.role,
                       worker_id='worker', status=TaskStatus.COMPLETE,
                       completed_at=now_iso())
        return task, run

    def test_empty_reconciliation_does_not_reserve_sqlite_writer(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp))
            job = store.create_job('empty completion journal')
            with patch.object(
                store,
                '_completion_scope',
                side_effect=AssertionError('reserved writer for empty journal'),
            ) as completion_scope:
                store.reconcile_completions(job.id)
            completion_scope.assert_not_called()

    def test_simultaneous_completions(self):
        for backend in (SwarmStore, SQLiteSwarmStore):
            for mode in ('different', 'duplicate', 'conflict'):
                with self.subTest(backend=backend.backend_name, mode=mode), TemporaryDirectory() as tmp:
                    store = backend(Path(tmp))
                    job = store.create_job('concurrent completion')
                    first = self.make_task(store, job)
                    second = self.make_task(store, job) if mode == 'different' else first
                    entered = threading.Event()
                    contended = threading.Event()
                    original_get = store._get_completion
                    original_acquire = store.acquire_lock
                    original_reserve = getattr(store, '_reserve_writer', None)

                    def get_completion(*args):
                        if not entered.is_set():
                            entered.set()
                            self.assertTrue(contended.wait(2), 'second writer never arrived')
                        return original_get(*args)

                    def acquire(name, owner, ttl_seconds=None):
                        result = original_acquire(name, owner, ttl_seconds)
                        if name.startswith('completion-intent:') and not result:
                            contended.set()
                        return result

                    def reserve(connection):
                        if entered.is_set():
                            contended.set()
                        return original_reserve(connection)

                    def complete(pair, changed=False):
                        task, run = pair
                        return store.complete_task(task, run, [], {'task_id': task.id, 'changed': changed})

                    reservation = patch.object(store, '_reserve_writer', side_effect=reserve) if original_reserve else patch.object(store, 'acquire_lock', side_effect=acquire)
                    with patch.object(store, '_get_completion', side_effect=get_completion), reservation:
                        with ThreadPoolExecutor(2) as pool:
                            a = pool.submit(complete, first)
                            self.assertTrue(entered.wait(2))
                            b = pool.submit(complete, second, mode == 'conflict')
                            a.result(timeout=5)
                            if mode == 'conflict':
                                with self.assertRaises(ContractConflict):
                                    b.result(timeout=5)
                            else:
                                b.result(timeout=5)
                    store.reconcile_completions(job.id)
                    ref = store.job_ref(job.id)
                    for task, run in (first, second):
                        self.assertEqual(store.get_task_by_id(task.id).status, TaskStatus.COMPLETE)
                        self.assertEqual(store.get_completion_receipt(ref, run.id).outcome, 'published')
                    events = [e for e in store.read_events(job.id) if e['event'] == 'worker.completed_task']
                    self.assertEqual(len(events), 2 if mode == 'different' else 1)
                    complete(first)
                    with self.assertRaises(ContractConflict):
                        complete(first, True)
                    stale = replace(first[1], id='stale', worker_id='foreign')
                    store.complete_task(first[0], stale, [], {})
                    self.assertIsNone(store._get_completion(job.id, stale.id))

    def test_intent_timeout_keeps_foreign_lock_and_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            job = store.create_job('busy')
            task, run = self.make_task(store, job)
            name = 'completion-intent:' + job.id
            self.assertTrue(store.acquire_lock(name, 'foreign', ttl_seconds=300))
            clock = [0.0]
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            with patch('puppetmaster.store.time.monotonic', side_effect=lambda: clock[0]), patch('puppetmaster.store.time.sleep', side_effect=sleep):
                with self.assertRaisesRegex(RuntimeError, 'completion intent busy'):
                    store.complete_task(task, run, [], {})
            self.assertAlmostEqual(clock[0], 5.0)
            self.assertLessEqual(len(sleeps), 501)
            self.assertFalse(store.acquire_lock(name, 'intruder', ttl_seconds=300))
            self.assertIsNone(store._get_completion(job.id, run.id))
            self.assertEqual(store.get_task_by_id(task.id).status, TaskStatus.RUNNING)
            store.release_lock(name, owner='foreign')
            self.assertEqual(store.complete_task(task, run, [], {}).status, TaskStatus.COMPLETE)

    def test_intent_body_errors_are_not_retried_and_lock_is_released(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            job = store.create_job('error')
            task, run = self.make_task(store, job)
            with patch.object(store, '_get_completion', side_effect=ContractConflict('conflict')) as read:
                with self.assertRaises(ContractConflict):
                    store.complete_task(task, run, [], {})
                self.assertEqual(read.call_count, 1)
            self.assertEqual(store.complete_task(task, run, [], {}).status, TaskStatus.COMPLETE)
