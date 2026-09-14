"""Completion intent contention must not lose accepted worker output."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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

    def test_reconciliation_reads_only_pending_records(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp))
            job = store.create_job('historical completions')
            task, run = self.make_task(store, job)
            store.complete_task(task, run, [], {'task_id': task.id})
            pending_task, pending_run = self.make_task(store, job)
            with patch.object(store, 'reconcile_completions'):
                store.complete_task(pending_task, pending_run, [], {'task_id': pending_task.id})
            original_all = store._all
            loaded = []

            def read(query, *args, **kwargs):
                rows = original_all(query, *args, **kwargs)
                if query.startswith('SELECT data FROM completions WHERE job_id = ?'):
                    loaded.extend(rows)
                return rows

            with patch.object(store, '_all', side_effect=read):
                store.reconcile_completions(job.id)
            # One unlocked probe and one authoritative read under the writer.
            self.assertEqual(len(loaded), 2)
            self.assertTrue(all(pending_run.id in row['data'] for row in loaded))
            self.assertEqual(len(store._completion_records(job.id)), 2)
            self.assertEqual(store.get_completion_receipt(store.job_ref(job.id), run.id).outcome,
                             'published')
            self.assertEqual(store.get_task_by_id(pending_task.id).status, TaskStatus.COMPLETE)
            loaded.clear()
            with patch.object(store, '_all', side_effect=read), \
                    patch.object(store, '_completion_scope', side_effect=AssertionError('no pending work')):
                store.reconcile_completions(job.id)
            self.assertEqual(loaded, [])

    def test_claim_sweep_skips_candidates_taken_since_snapshot(self):
        for backend in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=backend.backend_name), TemporaryDirectory() as tmp:
                store = backend(Path(tmp))
                job = store.create_job('stale queue snapshot')
                for _ in range(4):
                    store.save_task(Task(job_id=job.id, role='explore', instruction='claim'))
                original_list = store.list_tasks
                original_claim = store.claim_task
                stolen = []
                scanned = False

                def snapshot(job_id):
                    nonlocal scanned
                    tasks = original_list(job_id)
                    if scanned:
                        return tasks
                    scanned = True
                    # A peer wins every task after this reader takes its snapshot.
                    for task in tasks:
                        stolen.append(original_claim(task.id, 'peer'))
                    return tasks

                with patch.object(store, 'list_tasks', side_effect=snapshot), \
                        patch.object(store, 'claim_task', wraps=original_claim) as claim:
                    self.assertIsNone(store.claim_next_task(job.id, 'loser'))
                self.assertEqual(claim.call_count, 0)
                self.assertTrue(all(task.lease_owner == 'peer' for task in stolen))

    def test_reconciliation_refreshes_pending_records_under_publication_lock(self):
        for backend in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=backend.backend_name), TemporaryDirectory() as tmp:
                store = backend(Path(tmp))
                job = store.create_job('publication race')
                first, first_run = self.make_task(store, job)
                second, second_run = self.make_task(store, job)
                with patch.object(store, 'reconcile_completions'):
                    store.complete_task(first, first_run, [], {'task_id': first.id})
                peer = backend(Path(tmp))
                scope = store._completion_scope

                @contextmanager
                def raced_scope(job_id):
                    # The unlocked probe saw first; a peer publishes it and
                    # accepts second before this publisher acquires ownership.
                    peer.reconcile_completions(job_id)
                    with patch.object(peer, 'reconcile_completions'):
                        peer.complete_task(second, second_run, [], {'task_id': second.id})
                    with scope(job_id) as acquired:
                        yield acquired

                with patch.object(store, '_completion_scope', raced_scope), \
                        patch.object(store, 'save_run', wraps=store.save_run) as save:
                    store.reconcile_completions(job.id)
                self.assertEqual([call.args[0].id for call in save.call_args_list], [second_run.id])
                self.assertTrue(all(record['done'] for record in store._completion_records(job.id)))
                events = [e for e in store.read_events(job.id) if e['event'] == 'worker.completed_task']
                self.assertEqual(len(events), 2)

    def test_claim_race_after_advisory_read_still_respects_peer_lease(self):
        for backend in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=backend.backend_name), TemporaryDirectory() as tmp:
                store = backend(Path(tmp))
                job = store.create_job('claim race')
                task = Task(job_id=job.id, role='explore', instruction='claim')
                store.save_task(task)
                original_claim = store.claim_task

                def race(task_id, worker_id, **kwargs):
                    self.assertIsNotNone(original_claim(task_id, 'peer'))
                    return original_claim(task_id, worker_id, **kwargs)

                with patch.object(store, 'claim_task', side_effect=race):
                    self.assertIsNone(store.claim_next_task(job.id, 'loser'))
                current = store.get_task_by_id(task.id)
                self.assertEqual(current.lease_owner, 'peer')
                self.assertEqual(current.attempts, 1)

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
