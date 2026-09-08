"""Working duration excludes parked HOLD/wait time."""
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

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from puppetmaster.models import (
    Task,
    TaskStatus,
    apply_running_duration,
    task_working_seconds,
)
from puppetmaster.receipt import build_job_receipt
from puppetmaster.store import SwarmStore


class RunningDurationFoldTests(unittest.TestCase):
    def test_leaving_running_adds_claimed_interval(self) -> None:
        task = Task(
            job_id="job",
            role="explore",
            instruction="work",
            status=TaskStatus.RUNNING,
            claimed_at="2026-01-01T00:00:00+00:00",
        )
        folded = apply_running_duration(
            task, TaskStatus.COMPLETE, now="2026-01-01T00:00:12+00:00"
        )
        self.assertEqual(folded.working_seconds, 12.0)
        self.assertIsNone(folded.claimed_at)

    def test_entering_running_stamps_claimed_at(self) -> None:
        task = Task(job_id="job", role="explore", instruction="work")
        claimed = apply_running_duration(
            task, TaskStatus.RUNNING, now="2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(claimed.claimed_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(claimed.working_seconds, 0.0)

    def test_open_running_slice_counts_in_live_working(self) -> None:
        task = Task(
            job_id="job",
            role="explore",
            instruction="work",
            status=TaskStatus.RUNNING,
            claimed_at="2026-01-01T00:00:00+00:00",
            working_seconds=3.0,
        )
        self.assertEqual(
            task_working_seconds(task, now="2026-01-01T00:00:07+00:00"),
            10.0,
        )


class ReceiptWorkingParkedTests(unittest.TestCase):
    def test_receipt_splits_working_from_parked_wall_clock(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("duration")
            store.save_job(
                replace(
                    job,
                    created_at="2026-01-01T00:00:00+00:00",
                    completed_at="2026-01-01T00:01:00+00:00",
                )
            )
            task = Task(
                job_id=job.id,
                role="explore",
                instruction="work",
                status=TaskStatus.COMPLETE,
                working_seconds=15.0,
                created_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:15+00:00",
            )
            store.save_task(task)
            receipt = build_job_receipt(store, job.id)
            self.assertEqual(receipt["elapsed_seconds"], 60.0)
            self.assertEqual(receipt["working_seconds"], 15.0)
            self.assertEqual(receipt["parked_seconds"], 45.0)

    def test_complete_task_folds_claimed_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("fold on complete")
            claimed_at = (
                datetime.now(timezone.utc) - timedelta(seconds=2)
            ).isoformat(timespec="seconds")
            task = Task(
                job_id=job.id,
                role="explore",
                instruction="work",
                status=TaskStatus.RUNNING,
                claimed_at=claimed_at,
                lease_owner="w",
                lease_id="lease_fold",
            )
            store.save_task(task)
            store.update_task_status(task, TaskStatus.COMPLETE, worker_id="w")
            stored = store.get_task_by_id(task.id)
            self.assertGreater(stored.working_seconds, 0)
            self.assertIsNone(stored.claimed_at)
