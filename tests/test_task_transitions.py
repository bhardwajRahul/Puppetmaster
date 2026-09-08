"""Mechanical task status transitions."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.models import (
    IllegalTaskStatusTransition,
    Task,
    TaskStatus,
    assert_legal_task_transition,
)
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class TaskTransitionMapTests(unittest.TestCase):
    def test_same_status_is_idempotent(self) -> None:
        assert_legal_task_transition(TaskStatus.COMPLETE, TaskStatus.COMPLETE)

    def test_complete_cannot_return_to_running(self) -> None:
        with self.assertRaises(IllegalTaskStatusTransition):
            assert_legal_task_transition(TaskStatus.COMPLETE, TaskStatus.RUNNING)

    def test_update_task_status_rejects_illegal_edge(self) -> None:
        for store_type in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(store=store_type.backend_name):
                with TemporaryDirectory() as tmp:
                    store = store_type(Path(tmp) / ".puppetmaster")
                    store.init()
                    job = store.create_job("transitions")
                    task = Task(
                        job_id=job.id,
                        role="explore",
                        instruction="done",
                        status=TaskStatus.COMPLETE,
                    )
                    store.save_task(task)
                    with self.assertRaises(IllegalTaskStatusTransition):
                        store.update_task_status(task, TaskStatus.RUNNING)
                    self.assertEqual(
                        store.get_task_by_id(task.id).status,
                        TaskStatus.COMPLETE,
                    )

    def test_queued_can_complete_inline_without_running(self) -> None:
        assert_legal_task_transition(TaskStatus.QUEUED, TaskStatus.COMPLETE)

    def test_failed_can_requeue_for_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("retry")
            task = Task(
                job_id=job.id,
                role="explore",
                instruction="retry me",
                status=TaskStatus.FAILED,
            )
            store.save_task(task)
            updated = store.update_task_status(task, TaskStatus.QUEUED)
            self.assertEqual(updated.status, TaskStatus.QUEUED)
