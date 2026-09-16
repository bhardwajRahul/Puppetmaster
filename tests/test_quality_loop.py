"""Opt-in cleanup and bounded same-adapter review repair."""
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

from puppetmaster.models import Artifact, ArtifactType, Task, TaskStatus
from puppetmaster.quality_loop import (
    maybe_requeue_review_repair,
    review_loop_enabled,
    run_cleanup_pass,
)
from puppetmaster.sqlite_store import SQLiteSwarmStore


class QualityLoopTests(unittest.TestCase):
    def test_cleanup_skips_when_disabled_or_no_paths(self) -> None:
        task = Task(job_id="j", role="implement", instruction="x", payload={})
        arts = run_cleanup_pass(task, [])
        self.assertEqual(arts, [])
        task = Task(
            job_id="j",
            role="implement",
            instruction="x",
            payload={"cleanup": True},
        )
        arts = run_cleanup_pass(task, [])
        self.assertEqual(arts[-1].payload["kind"], "cleanup")
        self.assertEqual(arts[-1].payload["note"], "no edited paths")

    def test_cleanup_failure_does_not_raise(self) -> None:
        task = Task(
            job_id="j",
            role="implement",
            instruction="x",
            payload={"cleanup": True, "cwd": "."},
        )
        patch = Artifact(
            job_id="j",
            task_id=task.id,
            type=ArtifactType.PATCH,
            created_by="w",
            confidence=0.9,
            evidence=["test:patch"],
            payload={"change": "edit", "files": ["src/a.py"], "changed_files": ["src/a.py"]},
        )

        def boom(_command, **_kwargs):
            raise RuntimeError("ruff missing")

        arts = run_cleanup_pass(task, [patch], runner=boom)
        self.assertEqual(arts[-1].payload["result"] if False else arts[-1].payload.get("kind"), "cleanup")
        self.assertEqual(arts[-1].payload.get("note"), "cleanup failed; implement kept")

    def test_review_repair_requeues_same_adapter_with_reasons(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("repair")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="ship it",
                adapter="agentic",
                status=TaskStatus.FAILED,
                payload={"review_loop": True, "review_loop_limit": 2, "model": "cheap"},
            )
            store.save_task(task)
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=task.id,
                    type=ArtifactType.GATE,
                    created_by="reviewer",
                    confidence=0.9,
                    evidence=["test:review"],
                    payload={
                        "gate": "review",
                        "kind": "review",
                        "passed": False,
                        "reason": "missing test",
                    },
                )
            )
            repaired = maybe_requeue_review_repair(store, job.id)
            self.assertEqual(len(repaired), 1)
            self.assertEqual(repaired[0].status, TaskStatus.QUEUED)
            self.assertEqual(repaired[0].adapter, "agentic")
            self.assertTrue(repaired[0].payload["allow_dirty"])
            self.assertIn("missing test", repaired[0].instruction)
            self.assertTrue(review_loop_enabled(repaired[0].payload))

    def test_review_loop_does_not_multiply_with_model_escalation(self) -> None:
        from puppetmaster.orchestrator import Orchestrator

        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("no multiply")
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="x",
                adapter="agentic",
                status=TaskStatus.FAILED,
                payload={
                    "review_loop": True,
                    "router_model_id": "agentic/x",
                    "review_escalation_attempts": 0,
                },
            )
            store.save_task(task)
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=task.id,
                    type=ArtifactType.GATE,
                    created_by="reviewer",
                    confidence=0.9,
                    evidence=["test:review"],
                    payload={
                        "gate": "review",
                        "kind": "review",
                        "passed": False,
                        "reason": "nits",
                    },
                )
            )
            orch = Orchestrator(store)
            self.assertEqual(orch._reroute_failed_review(job), 0)
            self.assertIn(task.id, orch._review_pending_reroute_ids(job))
