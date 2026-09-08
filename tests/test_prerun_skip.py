"""Prerun skip: cheap skip before adapter spawn."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.models import JobStatus, Task, TaskStatus
from puppetmaster.prerun import prerun_skip_reason
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore
from puppetmaster.worker_runtime import WorkerRuntime


class PrerunSkipReasonTests(unittest.TestCase):
    def test_explicit_skip_uses_reason(self) -> None:
        task = Task(
            job_id="job",
            role="explore",
            instruction="already done",
            payload={"prerun": {"skip": True, "reason": "fingerprint match"}},
        )
        self.assertEqual(prerun_skip_reason(task), "fingerprint match")

    def test_missing_prerun_does_not_skip(self) -> None:
        task = Task(job_id="job", role="explore", instruction="work")
        self.assertIsNone(prerun_skip_reason(task))


class PrerunSkipRuntimeTests(unittest.TestCase):
    def test_skip_does_not_construct_local_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("skip prerun")
            store.update_job_status(job.id, JobStatus.RUNNING)
            task = Task(
                job_id=job.id,
                role="explore",
                instruction="already satisfied",
                payload={"prerun": {"skip": True, "reason": "reuse"}},
            )
            store.save_task(task)

            class _BoomWorker:
                def __init__(self, role, worker_id=None):
                    raise AssertionError("LocalWorker must not spawn on prerun skip")

            runtime = WorkerRuntime(
                store=store, job_id=job.id, role="explore", worker_id="w"
            )
            with patch("puppetmaster.worker_runtime.LocalWorker", _BoomWorker):
                self.assertTrue(runtime.run_once())
            stored = store.get_task_by_id(task.id)
            self.assertEqual(stored.status, TaskStatus.SKIPPED)
            self.assertIsNotNone(stored.completed_at)

    def test_skipped_dependency_unblocks_child(self) -> None:
        for store_type in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(store=store_type.backend_name):
                with TemporaryDirectory() as tmp:
                    store = store_type(Path(tmp) / ".puppetmaster")
                    store.init()
                    job = store.create_job("skip dependents")
                    parent = Task(
                        job_id=job.id,
                        role="explore",
                        instruction="parent",
                        status=TaskStatus.SKIPPED,
                    )
                    child = Task(
                        job_id=job.id,
                        role="review",
                        instruction="child",
                        depends_on=[parent.id],
                    )
                    store.save_task(parent)
                    store.save_task(child)
                    claimed = store.claim_next_task(job.id, "w", role="review")
                    self.assertIsNotNone(claimed)
                    self.assertEqual(claimed.id, child.id)
                    self.assertTrue(store.has_incomplete_tasks(job.id))
