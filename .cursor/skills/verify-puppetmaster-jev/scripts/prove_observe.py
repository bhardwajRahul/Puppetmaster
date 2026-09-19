#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
import hermetic_env  # noqa: F401

from puppetmaster.gist_admission import maybe_admit_finding_as_gist
from puppetmaster.jev.edges import (
    apply_already_answered,
    apply_conflict_auditor_gate,
    record_already_answered_on_job,
)
from puppetmaster.jev.render import render_transition_section
from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task, TaskStatus
from puppetmaster.stitcher import Stitcher
from puppetmaster.store import SwarmStore

OUT = Path("/tmp/verify-this/puppetmaster-jev")


def _fake_decide(state, questions, **kwargs):
    if "already_answered" in questions:
        return {"answers": {"already_answered": {"noul": 0.91}}}
    if "still_unanswered" in questions:
        return {"answers": {"still_unanswered": {"noul": 0.11}}}
    if "repo_fact" in questions:
        return {"answers": {"repo_fact": {"noul": 0.12}}}
    return {"answers": {name: {"noul": 0.08} for name in questions}}


def _run(*, act: bool) -> dict:
    os.environ["PUPPETMASTER_JEV"] = "1"
    os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
    if act:
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
    else:
        os.environ.pop("PUPPETMASTER_JEV_ACT", None)
    cwd = str(REPO)
    with TemporaryDirectory() as tmp, patch(
        "puppetmaster.jev.edges.decide", _fake_decide
    ):
        store = SwarmStore(Path(tmp) / ".puppetmaster")
        store.init()
        job = store.create_job("goal")
        store.update_job_status(job.id, JobStatus.RUNNING)
        explore = Task(
            job_id=job.id,
            role="explore",
            instruction="e",
            status=TaskStatus.COMPLETE,
            payload={"cwd": cwd},
        )
        auditor = Task(
            job_id=job.id,
            role="conflict-auditor",
            instruction="a",
            status=TaskStatus.QUEUED,
            depends_on=[explore.id],
            payload={"cwd": cwd},
        )
        store.save_task(explore)
        store.save_task(auditor)
        for index, claim in enumerate(("fact one", "fact two")):
            store.save_artifact(
                Artifact(
                    job_id=job.id,
                    task_id=explore.id,
                    type=ArtifactType.FINDING,
                    created_by="w",
                    confidence=0.9,
                    evidence=["x.py:%d" % index],
                    payload={"claim": claim},
                )
            )
        v1 = apply_conflict_auditor_gate(store, auditor)
        prior = store.create_job("prior")
        prior_task = Task(
            job_id=prior.id,
            role="explore",
            instruction="p",
            status=TaskStatus.COMPLETE,
            payload={"cwd": cwd},
        )
        store.save_task(prior_task)
        store.save_artifact(
            Artifact(
                job_id=prior.id,
                task_id=prior_task.id,
                type=ArtifactType.FINDING,
                created_by="w",
                confidence=0.9,
                evidence=["y.py:1"],
                payload={"claim": "old"},
            )
        )
        store.update_job_status(prior.id, JobStatus.COMPLETE)
        reused = apply_already_answered(store, "brand new windows question", cwd)
        launched = store.create_job("brand new windows question")
        launched_task = Task(
            job_id=launched.id,
            role="explore",
            instruction="today",
            status=TaskStatus.QUEUED,
            payload={"cwd": cwd},
        )
        store.save_task(launched_task)
        v2 = record_already_answered_on_job(
            store, launched.id, "brand new windows question", cwd, task_id=launched_task.id
        )
        admit_job = store.create_job("admit")
        admit_task = Task(
            job_id=admit_job.id,
            role="explore",
            instruction="f",
            status=TaskStatus.COMPLETE,
            payload={"cwd": cwd},
        )
        store.save_task(admit_task)
        finding = Artifact(
            job_id=admit_job.id,
            task_id=admit_task.id,
            type=ArtifactType.FINDING,
            created_by="w",
            confidence=0.9,
            evidence=["z.py:1"],
            payload={"claim": "worker completed TaskStatus.COMPLETE"},
        )
        store.save_artifact(finding)
        store.save_artifact(
            Artifact(
                job_id=admit_job.id,
                task_id=admit_task.id,
                type=ArtifactType.VERIFICATION,
                created_by="w",
                confidence=0.4,
                evidence=[finding.id],
                payload={"check": finding.payload["claim"], "result": "passed"},
            )
        )
        gist = maybe_admit_finding_as_gist(store, finding)
        follow = store.create_job("continue")
        store.update_job_status(follow.id, JobStatus.RUNNING)
        parent = Task(
            job_id=follow.id,
            role="explore",
            instruction="wave",
            status=TaskStatus.COMPLETE,
            payload={"cwd": cwd},
        )
        store.save_task(parent)
        enq = Artifact(
            job_id=follow.id,
            task_id=parent.id,
            type=ArtifactType.FINDING,
            created_by="w",
            confidence=0.9,
            evidence=["c.py:1"],
            payload={
                "claim": "old",
                "enqueue_subtasks": [{"role": "explore", "instruction": "again"}],
            },
        )
        store.save_artifact(enq)
        created = store.maybe_enqueue_follow_ups_from_artifact(
            enq, parent_task_id=parent.id, created_by="w"
        )
        prior_gates = [
            artifact
            for artifact in store.list_artifacts(prior.id)
            if artifact.type == ArtifactType.GATE
        ]
        launched_gates = [
            artifact
            for artifact in store.list_artifacts(launched.id)
            if artifact.type == ArtifactType.GATE
        ]
        v3_gate = next(
            (
                artifact
                for artifact in store.list_artifacts(admit_job.id)
                if artifact.type == ArtifactType.GATE
                and (artifact.payload or {}).get("edge") == "finding_admission"
            ),
            None,
        )
        v4_gate = next(
            (
                artifact
                for artifact in store.list_artifacts(follow.id)
                if artifact.type == ArtifactType.GATE
                and (artifact.payload or {}).get("edge") == "stop_spawn"
            ),
            None,
        )
        stitch = Stitcher(store).preview(job.id)
        unset_like = Stitcher(store).preview(prior.id)
        return {
            "act": act,
            "v1_auditor": str(store.get_task_by_id(auditor.id).status),
            "v1_would": v1.would_action,
            "v1_acted": v1.acted,
            "v2_reused": reused is not None,
            "v2_prior_gates": len(prior_gates),
            "v2_launched_gates": len(launched_gates),
            "v2_would": None if v2 is None else v2.would_action,
            "v2_acted": None if v2 is None else v2.acted,
            "v3_gist": gist is not None,
            "v3_would": None
            if v3_gate is None
            else (v3_gate.payload or {}).get("would_action"),
            "v3_acted": None
            if v3_gate is None
            else bool((v3_gate.payload or {}).get("acted")),
            "v4_followups": len(created),
            "v4_would": None
            if v4_gate is None
            else (v4_gate.payload or {}).get("would_action"),
            "v4_acted": None
            if v4_gate is None
            else bool((v4_gate.payload or {}).get("acted")),
            "stitch_has_jev": "## Jev" in stitch,
            "unset_like_has_jev": "## Jev" in unset_like,
            "observe_lines": render_transition_section(store.list_artifacts(job.id)),
        }


def main() -> int:
    observe = _run(act=False)
    act = _run(act=True)
    v1_acts_on_opt_in = (
        observe["v1_auditor"] == "skipped"
        and observe["v1_would"] == "skip"
        and observe["v1_acted"] is True
        and observe["stitch_has_jev"] is True
    )
    v234_match_today = (
        observe["v2_reused"] is False
        and observe["v3_gist"] is True
        and observe["v4_followups"] == 1
        and observe["v2_prior_gates"] == 0
        and observe["v2_launched_gates"] == 1
        and observe["v2_would"] == "skip"
        and observe["v3_would"] == "skip"
        and observe["v4_would"] == "skip"
        and not observe["v2_acted"]
        and observe["v3_acted"] is False
        and observe["v4_acted"] is False
        and observe["unset_like_has_jev"] is False
    )
    body = {
        "observe": observe,
        "act": act,
        "v1_acts_on_opt_in": v1_acts_on_opt_in,
        "v234_match_today": v234_match_today,
        "would_skip_recorded": (
            observe["v1_would"] == "skip"
            and observe["v2_would"] == "skip"
            and observe["v3_would"] == "skip"
            and observe["v4_would"] == "skip"
        ),
        "ok": v1_acts_on_opt_in
        and v234_match_today
        and act["v1_auditor"] == "skipped"
        and act["v2_reused"] is True
        and act["v3_gist"] is False
        and act["v4_followups"] == 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "observe.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(json.dumps(body, indent=2))
    return 0 if body["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
