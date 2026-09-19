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

from puppetmaster.jev.artifacts import build_transition_gate
from puppetmaster.jev.render import render_transition_section
from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task, TaskStatus
from puppetmaster.stitcher import Stitcher
from puppetmaster.store import SwarmStore


class JevRenderTests(unittest.TestCase):
    def test_unset_summary_has_no_jev_section(self) -> None:
        self.assertEqual(render_transition_section([]), [])

    def test_observe_line_names_would_and_graph(self) -> None:
        gate = build_transition_gate(
            job_id="job",
            task_id="task",
            edge="conflict_auditor",
            action="spawn",
            would_action="skip",
            acted=False,
            reason="no_pair_above_threshold",
            max_noul=0.3,
        )
        lines = render_transition_section([gate])
        self.assertEqual(lines[1], "## Jev")
        self.assertIn("would skip noul=0.30", lines[2])
        self.assertIn("ran", lines[2])

    def test_fail_open_is_visible(self) -> None:
        gate = build_transition_gate(
            job_id="job",
            task_id="task",
            edge="already_answered",
            action="spawn",
            would_action="spawn",
            acted=False,
            reason="fail_open",
        )
        lines = render_transition_section([gate])
        self.assertIn("fail-open", lines[2])
        self.assertIn("launched", lines[2])

    def test_stitcher_preview_includes_observe_section(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("preview jev")
            store.update_job_status(job.id, JobStatus.RUNNING)
            task = Task(
                job_id=job.id,
                role="explore",
                instruction="find",
                status=TaskStatus.COMPLETE,
            )
            store.save_task(task)
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=task.id,
                    type=ArtifactType.FINDING,
                    created_by="w",
                    confidence=0.9,
                    evidence=["x.py:1"],
                    payload={"claim": "a repository fact"},
                )
            )
            store.save_artifact(
                build_transition_gate(
                    job_id=job.id,
                    task_id=task.id,
                    edge="finding_admission",
                    action="spawn",
                    would_action="skip",
                    acted=False,
                    reason="plumbing_demoted",
                    max_noul=0.46,
                )
            )
            text = Stitcher(store).preview(job.id)
        self.assertIn("## Jev", text)
        self.assertIn("finding-admission: would skip noul=0.46 — admitted", text)
        self.assertIn("## Findings", text)
        findings = text.split("## Findings")[1].split("## Conflicts")[0]
        self.assertIn("a repository fact", findings)


if __name__ == "__main__":
    unittest.main()
