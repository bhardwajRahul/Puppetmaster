"""Job deletion removes journals and ledgers atomically and only for that job."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic_env  # noqa: F401

from puppetmaster.attempts import ExecutionAttempt, UsageObservation
from puppetmaster.models import AgentRun, Artifact, ArtifactType, Task, to_jsonable
from puppetmaster.sqlite_store import SQLiteSwarmStore


class SQLiteJobDeletionTests(unittest.TestCase):
    tables = (
        "completions", "usage_observations", "execution_attempts",
        "events", "artifacts", "runs", "tasks", "graph_edges", "jobs",
    )

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SQLiteSwarmStore(Path(tmp.name) / "state")
        self.store.init()
        self.target = self.populate_job("delete me")
        self.other = self.populate_job("keep me")

    def populate_job(self, goal):
        job = self.store.create_job(goal)
        for done in (False, True):
            task = Task(job_id=job.id, role="implement", instruction="test")
            self.store.save_task(task)
            run = AgentRun(job_id=job.id, task_id=task.id,
                           role=task.role, worker_id="worker")
            self.store.save_run(run)
            artifact = Artifact(job_id=job.id, task_id=task.id,
                                type=ArtifactType.FINDING, created_by="worker",
                                payload={"claim": "test"}, confidence=1.0, evidence=["test"])
            self.store.save_artifact(artifact)
            attempt = ExecutionAttempt.from_run(run, adapter="codex")
            self.store.record_attempt(attempt)
            self.store.record_usage_observation(UsageObservation(
                job_id=job.id, attempt_id=attempt.attempt_id,
                observation_id="usage", source="codex", observed_at=run.started_at,
            ))
            self.store._save_completion(job.id, {
                "task": to_jsonable(task), "run": to_jsonable(run),
                "artifacts": [to_jsonable(artifact)], "event_payload": {}, "done": done,
            })
        return job

    def rows(self, job_id):
        with self.store._session() as db:
            return {
                table: [tuple(row) for row in db.execute(
                    f"SELECT * FROM {table} WHERE {'id' if table == 'jobs' else 'job_id'} = ? ORDER BY rowid",
                    (job_id,),
                )]
                for table in self.tables
            }

    def test_delete_pending_and_completed_journals_and_job_data(self):
        before = self.rows(self.target.id)
        other_before = self.rows(self.other.id)
        for table, rows in before.items():
            self.assertTrue(rows, table)
        self.assertEqual(
            {record["done"] for record in self.store._completion_records(self.target.id)},
            {False, True},
        )
        self.store.delete_job(self.target.id)
        for table, rows in self.rows(self.target.id).items():
            self.assertEqual(rows, [], table)
        self.assertEqual(self.store._completion_records(self.target.id), [])
        self.assertFalse(self.store.job_dir(self.target.id).exists())
        self.assertEqual(self.rows(self.other.id), other_before)

    def test_delete_failure_rolls_back_journals_and_all_job_rows(self):
        before = self.rows(self.target.id)
        other_before = self.rows(self.other.id)
        job_dir = self.store.job_dir(self.target.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        marker = job_dir / "preserved.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.store._session() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            db.execute("""CREATE TRIGGER reject_job_delete BEFORE DELETE ON jobs
                          BEGIN SELECT RAISE(ABORT, 'delete rejected'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "delete rejected"):
            self.store.delete_job(self.target.id)
        self.assertEqual(self.rows(self.target.id), before)
        self.assertEqual(self.rows(self.other.id), other_before)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
