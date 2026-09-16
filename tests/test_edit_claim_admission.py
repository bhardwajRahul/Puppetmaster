from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from puppetmaster.edit_admission import EditAdmissionTimeout, edit_admission
from puppetmaster.file_claims import FileClaimRegistry
from puppetmaster.adapters.registry import ADAPTERS


@dataclass
class Task:
    job_id: str = "job-1"
    id: str = "task-1"
    adapter: str = "shell"
    payload: dict = None
    generation: int = 3
    lease_id: str = "lease-1"

    def __post_init__(self):
        if self.payload is None:
            self.payload = {}


class Store:
    def __init__(self):
        self.events = []

    def emit(self, job_id, event, payload):
        self.events.append((job_id, event, payload))


class EditAdmissionTests(unittest.TestCase):
    def test_declared_scope_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "claims.sqlite3"
            store = Store()
            task = Task(payload={"cwd": str(root), "write_scope": ["src/a", "src", "src/b"]})
            with patch("puppetmaster.edit_admission.default_file_claim_db_path", return_value=db):
                with edit_admission(store, task, "worker") as owner:
                    self.assertEqual(("src",), tuple(c.path for c in owner.claims))
                    self.assertTrue(owner.check())
                    self.assertEqual(task.generation, owner.generation)
                self.assertEqual([], FileClaimRegistry(db).list_active(root))
            self.assertEqual("edit_admission.released", store.events[-1][1])

    def test_read_only_matrix_does_not_claim_and_unknown_is_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            root, db = Path(directory), Path(directory) / "claims.sqlite3"
            with patch("puppetmaster.edit_admission.default_file_claim_db_path", return_value=db):
                for adapter, payload, expected in [
                    ("local", {}, 0),
                    ("openai", {}, 0),
                    ("shell", {"read_only": True}, 1),
                    ("codex", {"sandbox": "read-only", "dangerously_bypass_approvals_and_sandbox": True}, 1),
                    ("claude-code", {"permission_mode": "plan"}, 0),
                    ("claude-code", {"permission_mode": "acceptEdits"}, 1),
                    ("agy", {"mode": "plan"}, 0),
                    ("agy", {"mode": "accept-edits"}, 1),
                    ("future-adapter", {}, 1),
                ]:
                    task = Task(adapter=adapter, payload={"cwd": str(root), **payload})
                    with edit_admission(Store(), task, "worker") as owner:
                        self.assertEqual(expected, len(owner.claims), adapter)
                # Keep this test coupled to the real registry: adding an
                # adapter cannot silently escape the admission matrix.
                for adapter in ADAPTERS:
                    task = Task(adapter=adapter, payload={"cwd": str(root)})
                    with edit_admission(Store(), task, "worker") as owner:
                        self.assertIn(len(owner.claims), (0, 1), adapter)

    def test_managed_claim_renews_past_ttl_and_conflict_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root, db = Path(directory), Path(directory) / "claims.sqlite3"
            task = Task(payload={"cwd": str(root), "edit_claim_ttl_seconds": 0.1})
            with patch("puppetmaster.edit_admission.default_file_claim_db_path", return_value=db):
                first = edit_admission(Store(), task, "one")
                try:
                    time.sleep(0.25)
                    self.assertTrue(first.check())
                    other = Task(id="task-2", payload={"cwd": str(root), "edit_admission_wait_seconds": 0.1})
                    with self.assertRaises(EditAdmissionTimeout):
                        edit_admission(Store(), other, "two")
                finally:
                    first.close()

    def test_exception_releases_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root, db = Path(directory), Path(directory) / "claims.sqlite3"
            task = Task(payload={"cwd": str(root)})
            with patch("puppetmaster.edit_admission.default_file_claim_db_path", return_value=db):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    with edit_admission(Store(), task, "worker"):
                        raise RuntimeError("boom")
                self.assertEqual([], FileClaimRegistry(db).list_active(root))


if __name__ == "__main__":
    unittest.main()
