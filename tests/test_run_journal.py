"""Run journal crash-recovery stamps and resume attempt budget."""
from __future__ import annotations

import os
import sys

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

import unittest
from tempfile import TemporaryDirectory

from puppetmaster.host_lifecycle import (
    HOST_EVENT_RECOVERED,
    classify_host_start,
    record_host_start,
    reset_host_start_guard,
)
from puppetmaster.models import JobStatus
from puppetmaster.run_journal import (
    MAX_AUTO_RESUME,
    RunJournal,
    list_stale_job_ids,
    recover_stale_journals,
)
from puppetmaster.store import SwarmStore


class RunJournalTests(unittest.TestCase):
    def test_stale_until_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = RunJournal(tmp, "job_a")
            journal.append("turn.started", {"turn": 1})
            self.assertTrue(journal.is_stale())
            self.assertEqual(list_stale_job_ids(tmp), ["job_a"])
            closed = journal.stamp_aborted(reason="test")
            self.assertIsNotNone(closed)
            self.assertEqual(closed.kind, "aborted")
            self.assertFalse(journal.is_stale())
            self.assertEqual(list_stale_job_ids(tmp), [])

    def test_resume_attempt_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = RunJournal(tmp, "job_b")
            self.assertTrue(journal.may_auto_resume())
            for _ in range(MAX_AUTO_RESUME):
                journal.note_resume_attempt()
            self.assertFalse(journal.may_auto_resume())
            journal.clear_resume_attempts()
            self.assertTrue(journal.may_auto_resume())

    def test_torn_line_tolerated(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = RunJournal(tmp, "job_c")
            journal.append("heartbeat", {})
            # Simulate torn write
            with journal._path.open("a", encoding="utf-8") as handle:
                handle.write('{"seq": 2, "kind": "partial"')
            events = journal.replay()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "heartbeat")

    def test_recover_stale_journals_emits(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(tmp)
            store.init()
            job = store.create_job("recover me")
            journal = RunJournal(tmp, job.id)
            journal.append("turn.started", {})
            stamps = recover_stale_journals(store, reason="unit")
            self.assertEqual(len(stamps), 1)
            self.assertTrue(stamps[0].aborted)
            events = [e.get("event") for e in store.read_events(job.id)]
            self.assertIn("run.journal.aborted", events)

    def test_host_recovered_stamps_journals(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(tmp)
            store.init()
            job = store.create_job("live job")
            store.update_job_status(job.id, JobStatus.RUNNING)
            RunJournal(tmp, job.id).append("turn.started", {})
            # Prior unclean boot
            boot = tmp + "/host_boot.json"
            Path = __import__("pathlib").Path
            Path(boot).write_text(
                '{"clean_shutdown": false, "boot_id": "boot_old", "pid": 1, '
                '"host": "x", "started_at": "t", "kind": "host.started", '
                '"reason": "first"}\n',
                encoding="utf-8",
            )
            reset_host_start_guard()
            record = record_host_start(store)
            self.assertIsNotNone(record)
            self.assertEqual(record.kind, HOST_EVENT_RECOVERED)
            self.assertFalse(RunJournal(tmp, job.id).is_stale())


class ClassifyCrashTests(unittest.TestCase):
    def test_unclean_is_crash(self) -> None:
        kind, reason = classify_host_start({"clean_shutdown": False})
        self.assertEqual(kind, HOST_EVENT_RECOVERED)
        self.assertEqual(reason, "crash")


if __name__ == "__main__":
    unittest.main()
