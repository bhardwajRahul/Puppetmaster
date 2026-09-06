"""Regressions for the v1.24 candidate review blockers."""
import json
import os
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
import test_store_contracts
from puppetmaster.cancellation import request_cancel, check_cancellation, JobCancelled
from puppetmaster.invocation import execution_scope
from puppetmaster.models import Artifact, ArtifactType, JobRef, JobStatus, Task, TaskStatus, to_jsonable
from puppetmaster.projections import connection
from puppetmaster.store import SwarmStore
from puppetmaster.store_contracts import task_binding
from puppetmaster.win_process import popen_owned, cleanup_owned_process, close_owned_process


class CandidateRepairTests(unittest.TestCase):
    stores = test_store_contracts.StoreContractTests.stores

    def test_sqlite_reassignment_removes_old_refs_and_moves_counts(self):
        for store, job, task, run, ref in self.stores():
            if store.backend_name != "sqlite":
                continue
            other = store.create_job("destination")
            artifact = Artifact(job_id=job.id, task_id=task.id, created_by="worker",
                                type=ArtifactType.FINDING, payload={"claim": "probe"},
                                confidence=1.0, evidence=["tests/test_candidate_repairs.py"])
            store.save_artifact(artifact)
            before = store.list_job_summaries().revision
            # Exercise source UPDATE exactly, not a delete/insert approximation.
            with store._session() as c:
                for table, value in (("tasks", task), ("artifacts", artifact)):
                    moved = replace(value, job_id=other.id)
                    c.execute(f"UPDATE {table} SET job_id=?,data=? WHERE id=?",
                              (other.id, json.dumps(to_jsonable(moved)), value.id))
            self.assertEqual(store.list_task_refs(ref).items, ())
            self.assertEqual(store.list_artifact_refs(ref).items, ())
            other_ref = JobRef(other.id, ref.state_id)
            self.assertEqual(store.list_task_refs(other_ref).items[0].id, task.id)
            self.assertEqual(store.list_artifact_refs(other_ref).items[0].id, artifact.id)
            counts = {i.id: (i.task_count, i.artifact_count) for i in store.list_job_summaries().items}
            self.assertEqual(counts[job.id], (0, 0))
            self.assertEqual(counts[other.id], (1, 1))
            with connection(store) as c:
                for kind, value in (("task", task), ("artifact", artifact)):
                    rows = c.execute("SELECT job_id,deleted FROM projection_changes WHERE kind=? AND id=? AND revision>? ORDER BY revision",
                                     (kind, value.id, before)).fetchall()
                    self.assertEqual([tuple(r) for r in rows], [(job.id, 1), (other.id, 0)])

    def test_legacy_cancel_inside_execution_scope(self):
        for store, job, task, run, ref in self.stores():
            with execution_scope(store, run, task):
                request_cancel(job.id)
                with self.assertRaises(JobCancelled):
                    check_cancellation()
            successor = replace(task, generation=task.generation + 1, lease_id="next")
            with execution_scope(store, run, successor):
                check_cancellation()

    def test_legacy_cancel_from_host_thread_targets_active_binding(self):
        import threading
        for store, job, task, run, ref in self.stores():
            with execution_scope(store, run, task):
                thread = threading.Thread(target=request_cancel, args=(job.id,))
                thread.start()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                with self.assertRaises(JobCancelled):
                    check_cancellation()

    def test_terminal_and_running_request_finishes_observed(self):
        for store, job, task, run, ref in self.stores():
            terminal = replace(task, id="terminal", status=TaskStatus.COMPLETE)
            store.save_task(terminal)
            store.request_cancellation(ref, "mixed", [task_binding(terminal), task_binding(task)])
            store.observe_cancellation(ref, task_binding(task))
            self.assertEqual(store.get_cancellation_receipt(ref, "mixed").outcome, "observed_stop")

    def test_cancelled_candidate_does_not_starve_other_task(self):
        for store, job, task, run, ref in self.stores():
            first = replace(task, id="a", status=TaskStatus.QUEUED, lease_id=None, lease_owner=None)
            second = replace(first, id="b")
            store.save_task(first)
            store.save_task(second)
            store.request_cancellation(ref, "first", [task_binding(first)])
            with patch.object(store, "list_tasks", return_value=[first, second]):
                self.assertEqual(store.claim_next_task(job.id, "worker").id, second.id)

    def test_file_repair_emits_old_scope_and_status_leaves(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            job = store.create_job("repair", origin="old")
            before = store.list_job_summaries().revision
            # Simulate a source rename that committed before the index write.
            path = store.job_dir(job.id) / "job.json"
            path.write_text(json.dumps(to_jsonable(replace(job, origin="new", status=JobStatus.COMPLETE))))
            store.repair_metadata_index()
            for filters in ({"origin": "old"}, {"status": "queued"}, {"origin": "old", "status": "queued"}):
                changes = store.read_job_summary_changes(after_revision=before, **filters)
                self.assertEqual([(i.id, i.deleted) for i in changes.items], [(job.id, True)])
            self.assertEqual(store.list_job_summaries(origin="new").items[0].id, job.id)

    def test_windows_assigns_before_resume_and_never_uses_pid_cleanup(self):
        process = MagicMock()
        process.poll.return_value = 0
        with patch("puppetmaster.win_process.os.name", "nt"), \
             patch("puppetmaster.win_process.WindowsJob") as job_type, \
             patch("puppetmaster.win_process.subprocess.Popen", return_value=process) as launch, \
             patch("puppetmaster.win_process._toolhelp_kill_process_tree", side_effect=AssertionError("unsafe")), \
             patch("puppetmaster.win_process._taskkill_process_tree", side_effect=AssertionError("unsafe")):
            self.assertIs(popen_owned(["test"]), process)
            self.assertTrue(launch.call_args.kwargs["creationflags"] & 4)
            job_type.return_value.assign_and_resume.assert_called_once_with(process)
            cleanup_owned_process(process, "nonce", time.monotonic() + 1)
            job_type.return_value.terminate.assert_called_once()
            close_owned_process(process)
            job_type.return_value.close.assert_called_once()

    def test_windows_native_api_order_and_handle_width(self):
        import ctypes
        from puppetmaster.win_process import WindowsJob
        api, native = MagicMock(), MagicMock()
        api.CreateJobObjectW.return_value = 2 ** 40
        api.AssignProcessToJobObject.return_value = 1
        native.NtResumeProcess.return_value = 0
        sequence = MagicMock()
        sequence.attach_mock(api.AssignProcessToJobObject, "assign")
        sequence.attach_mock(native.NtResumeProcess, "resume")
        with patch.object(ctypes, "WinDLL", create=True, side_effect=[api, native]):
            job = WindowsJob()
            process = MagicMock(_handle=2 ** 41)
            job.assign_and_resume(process)
            self.assertEqual([c[0] for c in sequence.mock_calls], ["assign", "resume"])
            api.AssignProcessToJobObject.assert_called_once_with(2 ** 40, 2 ** 41)
            self.assertIs(api.CreateJobObjectW.restype, ctypes.c_void_p)
            api.SetInformationJobObject.assert_called_once()
            job.terminate()
            job.close()
            job.close()
            api.CloseHandle.assert_called_once_with(2 ** 40)

    def test_windows_assignment_failure_kills_suspended_handle(self):
        process = MagicMock()
        with patch("puppetmaster.win_process.os.name", "nt"), \
             patch("puppetmaster.win_process.WindowsJob") as job_type, \
             patch("puppetmaster.win_process.subprocess.Popen", return_value=process):
            job_type.return_value.assign_and_resume.side_effect = OSError("denied")
            with self.assertRaises(OSError):
                popen_owned(["test"])
            process.kill.assert_called_once()
            job_type.return_value.close.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "native Windows required")
    def test_native_windows_descendant_dies_after_leader_exit(self):
        child = "import time; time.sleep(60)"
        parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('ready',flush=True)"
        process = popen_owned([sys.executable, "-c", parent], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.wait(timeout=10)
            cleanup_owned_process(process, "nonce", time.monotonic() + 3)
            import threading
            output = []
            reader = threading.Thread(target=lambda: output.append(process.stdout.read()), daemon=True)
            reader.start()
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive(), "descendant still holds inherited stdout")
            self.assertEqual(output, [""])
        finally:
            close_owned_process(process)
            process.stdout.close()
