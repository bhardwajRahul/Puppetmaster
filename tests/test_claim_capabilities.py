"""Capability-aware claim and read-only peek."""
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

from puppetmaster.models import Task, TaskStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore, task_matches_capabilities


class ClaimCapabilityTests(unittest.TestCase):
    def test_empty_adapter_allowlist_matches_nothing(self) -> None:
        task = Task(job_id="j", role="explore", instruction="x", adapter="cursor")
        self.assertTrue(task_matches_capabilities(task, None))
        self.assertFalse(task_matches_capabilities(task, {"adapters": []}))
        self.assertTrue(task_matches_capabilities(task, {"adapters": ["cursor"]}))
        self.assertFalse(task_matches_capabilities(task, {"adapters": ["codex"]}))

    def test_placement_is_optional_unless_the_task_requires_it(self) -> None:
        free = Task(job_id="j", role="explore", instruction="x", adapter="local")
        pinned = Task(
            job_id="j",
            role="explore",
            instruction="x",
            adapter="local",
            payload={"placement": "remote"},
        )
        self.assertTrue(task_matches_capabilities(free, {"labels": ["local"]}))
        self.assertFalse(task_matches_capabilities(pinned, {"labels": ["local"]}))
        self.assertTrue(task_matches_capabilities(pinned, {"labels": ["remote"]}))

    def test_peek_does_not_claim_or_block(self) -> None:
        for store_type in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(store=store_type.backend_name):
                with TemporaryDirectory() as tmp:
                    store = store_type(Path(tmp) / ".puppetmaster")
                    store.init()
                    job = store.create_job("peek")
                    parent = Task(
                        job_id=job.id,
                        role="explore",
                        instruction="parent",
                        adapter="cursor",
                        status=TaskStatus.QUEUED,
                    )
                    child = Task(
                        job_id=job.id,
                        role="audit",
                        instruction="child",
                        adapter="codex",
                        depends_on=["pending-parent"],
                        status=TaskStatus.QUEUED,
                    )
                    store.save_task(parent)
                    store.save_task(child)
                    peeked = store.peek_next_task(
                        job.id,
                        "w-1",
                        capabilities={"adapters": ["cursor"]},
                    )
                    self.assertIsNotNone(peeked)
                    self.assertEqual(peeked.id, parent.id)
                    self.assertEqual(store.get_task_by_id(parent.id).status, TaskStatus.QUEUED)
                    self.assertEqual(store.get_task_by_id(child.id).status, TaskStatus.QUEUED)
                    missed = store.peek_next_task(
                        job.id,
                        "w-1",
                        capabilities={"adapters": ["hermes"]},
                    )
                    self.assertIsNone(missed)
                    claimed = store.claim_next_task(
                        job.id,
                        "w-1",
                        capabilities={"adapters": ["cursor"]},
                    )
                    self.assertEqual(claimed.id, parent.id)
                    self.assertEqual(claimed.status, TaskStatus.RUNNING)
                    self.assertIsNone(
                        store.claim_next_task(
                            job.id,
                            "w-2",
                            capabilities={"adapters": ["cursor"]},
                        )
                    )
