"""Explicit abort/continue/retry failure edges."""
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

from puppetmaster.failure_policy import normalize_failure_policy, task_failure_policy
from puppetmaster.models import Artifact, ArtifactType, Task, TaskStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.workers import WorkerSpec


class FailurePolicyNormalizeTests(unittest.TestCase):
    def test_spellings_and_retry_cap(self) -> None:
        self.assertEqual(normalize_failure_policy("abort")["action"], "abort")
        self.assertEqual(normalize_failure_policy("continue")["action"], "continue")
        self.assertEqual(normalize_failure_policy("retry(3)")["retries"], 3)
        with self.assertRaises(ValueError):
            normalize_failure_policy("retry(11)")
        with self.assertRaises(ValueError):
            normalize_failure_policy("retry(-1)")
        with self.assertRaises(ValueError):
            normalize_failure_policy({"action": "retry", "retries": True})

    def test_task_payload_roundtrip(self) -> None:
        self.assertIsNone(task_failure_policy({}))
        self.assertEqual(
            task_failure_policy({"failure_policy": "continue"})["action"],
            "continue",
        )


class FailurePolicyStoreTests(unittest.TestCase):
    def _store(self, tmp: str) -> SQLiteSwarmStore:
        store = SQLiteSwarmStore(Path(tmp) / ".puppetmaster")
        store.init()
        return store

    def test_continue_skips_and_does_not_fail_closed(self) -> None:
        from puppetmaster.orchestrator import Orchestrator

        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("fail continue")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="x",
                adapter="local",
                status=TaskStatus.FAILED,
                payload={"failure_policy": "continue"},
            )
            store.save_task(task)
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=task.id,
                    type=ArtifactType.VERIFICATION,
                    created_by="t",
                    confidence=0.9,
                    evidence=["test:failure"],
                    payload={"check": "run", "result": "failed", "failure": "task_failed"},
                )
            )
            changed = store.apply_pending_failure_policies(job.id)
            self.assertEqual(changed[0].status, TaskStatus.SKIPPED)
            orch = Orchestrator(store)
            self.assertFalse(orch._should_fail_closed(job, {task.id}))

    def test_retry_then_abort(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            job = store.create_job("fail retry")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="x",
                adapter="local",
                status=TaskStatus.FAILED,
                payload={"failure_policy": "retry(1)"},
            )
            store.save_task(task)
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=task.id,
                    type=ArtifactType.VERIFICATION,
                    created_by="t",
                    confidence=0.9,
                    evidence=["test:failure"],
                    payload={"check": "run", "result": "failed", "failure": "task_failed"},
                )
            )
            first = store.apply_failure_policy(task.id)
            self.assertEqual(first.status, TaskStatus.QUEUED)
            from dataclasses import replace

            store.save_task(replace(first, status=TaskStatus.FAILED))
            second = store.apply_failure_policy(first.id)
            self.assertEqual(second.status, TaskStatus.FAILED)
            self.assertEqual(second.payload["failure_policy_last_decision"]["action"], "abort")

    def test_spec_on_fail_is_normalized(self) -> None:
        spec = WorkerSpec(role="local", instruction="x", on_fail="abort")
        self.assertEqual(normalize_failure_policy(spec.on_fail)["action"], "abort")
