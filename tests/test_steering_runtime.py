"""Universal steering: ledger drain, receipts, successor spawn."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.models import Task, TaskStatus
from puppetmaster.session_commands import SessionCommandKind, SessionCommandStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.steering import (
    BOUNDARY_POST_OUTPUT,
    BOUNDARY_PRE_DISPATCH,
    STATUS_QUEUED_FOR_SUCCESSOR,
    apply_steering_to_task,
    broadcast_steer,
    drain_pending,
    enqueue_steer,
    spawn_successors_for_job,
)


class SteeringRuntimeTests(unittest.TestCase):
    def _store(self, tmp: str) -> SQLiteSwarmStore:
        store = SQLiteSwarmStore(Path(tmp) / ".puppetmaster")
        store.init()
        return store

    def test_pre_dispatch_applies_and_receipts(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("steer me")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="original",
                adapter="local",
                status=TaskStatus.RUNNING,
            )
            store.save_task(task)
            entry = enqueue_steer(store, job.id, "also run tests", task_id=task.id)
            texts = drain_pending(store, task, boundary=BOUNDARY_PRE_DISPATCH)
            self.assertEqual(texts, ["also run tests"])
            updated = apply_steering_to_task(task, texts)
            self.assertIn("also run tests", updated.instruction)
            names = [event.get("event") for event in store.read_events(job.id)]
            self.assertIn("steer.queued", names)
            self.assertIn("steer.applied", names)
            from puppetmaster.session_commands import ledger_for_store

            saved = ledger_for_store(store, job.id).list_entries()
            self.assertEqual(saved[0].status, SessionCommandStatus.APPLIED)
            self.assertEqual(saved[0].id, entry["id"])

    def test_post_output_queues_successor(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("late steer")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="done",
                adapter="codex",
                status=TaskStatus.COMPLETE,
            )
            store.save_task(task)
            enqueue_steer(store, job.id, "one more file", task_id=task.id)
            self.assertEqual(drain_pending(store, task, boundary=BOUNDARY_POST_OUTPUT), [])
            from puppetmaster.session_commands import ledger_for_store

            entry = ledger_for_store(store, job.id).list_entries()[0]
            self.assertEqual(entry.resolution, STATUS_QUEUED_FOR_SUCCESSOR)
            children = spawn_successors_for_job(store, job.id)
            self.assertEqual(len(children), 1)
            self.assertTrue(children[0].payload["steering_successor"])
            self.assertIn("one more file", children[0].instruction)
            self.assertIn(task.id, children[0].depends_on)

    def test_broadcast_fans_out_live_tasks_only(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("fanout")
            live = Task(
                job_id=job.id,
                role="a",
                instruction="a",
                status=TaskStatus.RUNNING,
            )
            done = Task(
                job_id=job.id,
                role="b",
                instruction="b",
                status=TaskStatus.COMPLETE,
            )
            store.save_task(live)
            store.save_task(done)
            entries = broadcast_steer(store, job.id, "stop and summarize")
            targets = [
                item.get("payload", {}).get("task_id")
                for item in entries
                if not item.get("payload", {}).get("record_only")
            ]
            self.assertEqual(targets, [live.id])

    def test_interrupt_requests_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("interrupt")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="x",
                status=TaskStatus.RUNNING,
            )
            store.save_task(task)
            enqueue_steer(
                store,
                job.id,
                "stop",
                task_id=task.id,
                kind=SessionCommandKind.INTERRUPT,
            )
            with mock.patch("puppetmaster.cancellation.request_cancel") as cancel:
                drain_pending(store, task, boundary=BOUNDARY_PRE_DISPATCH)
                cancel.assert_called_once_with(job.id)
