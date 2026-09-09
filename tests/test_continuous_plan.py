from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.adapters._prompts import (
    TASK_INSTRUCTION_HEADER,
    build_implement_prompt,
    structured_prompt_for_task,
)
from puppetmaster.continuous_plan import (
    DEFAULT_FANOUT_MAX,
    REASON_INTENT_SPEC,
    intent_spec_for_job,
    is_planner_task,
    maybe_requeue_planner,
    parse_handoff,
    parse_intent_spec,
    validate_handoff,
    validate_intent_spec,
)
from puppetmaster.models import AgentRun, Artifact, ArtifactType, Task, TaskStatus, now_iso
from puppetmaster.store import SwarmStore


def _events(store: SwarmStore, job_id: str, name: str) -> list[dict]:
    return [event for event in store.read_events(job_id) if event.get("event") == name]


def _finding(job_id: str, task_id: str, extra: dict) -> Artifact:
    payload = {"claim": "worker finished"}
    payload.update(extra)
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.FINDING,
        created_by="worker",
        confidence=0.9,
        evidence=["mod.py:1"],
        payload=payload,
    )


def _decision(job_id: str, task_id: str, payload: dict) -> Artifact:
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.DECISION,
        created_by="planner",
        confidence=0.9,
        evidence=["intent"],
        payload=payload,
    )


class ContractTests(unittest.TestCase):
    def test_handoff_allows_empty_lists(self) -> None:
        parsed = validate_handoff({"done": "landed the slice", "deviations": [], "concerns": []})
        self.assertEqual(parsed["done"], "landed the slice")
        self.assertEqual(parse_handoff(_finding("j", "t", parsed))["concerns"], [])

    def test_intent_rejects_bool_and_inverted_range(self) -> None:
        with self.assertRaises(ValueError):
            validate_intent_spec(
                {
                    "schema_version": 1,
                    "kind": "intent_spec",
                    "architecture": "modules",
                    "out_of_scope": [],
                    "dependency_philosophy": "stdlib",
                    "resource_timeouts": "30s tests",
                    "fanout_min": True,
                    "fanout_max": 8,
                }
            )
        with self.assertRaises(ValueError):
            validate_intent_spec(
                {
                    "schema_version": 1,
                    "kind": "intent_spec",
                    "architecture": "modules",
                    "out_of_scope": [],
                    "dependency_philosophy": "stdlib",
                    "resource_timeouts": "30s tests",
                    "fanout_min": 20,
                    "fanout_max": 2,
                }
            )
        self.assertIsNone(
            parse_intent_spec(
                {
                    "schema_version": 1,
                    "kind": "intent_spec",
                    "architecture": "modules",
                    "out_of_scope": [],
                    "dependency_philosophy": "stdlib",
                    "resource_timeouts": "30s tests",
                    "fanout_min": 20,
                    "fanout_max": 2,
                }
            )
        )


class PlannerFollowUpTests(unittest.TestCase):
    def test_planner_without_intent_refuses_worker_fanout(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("continuous")
            planner = Task(
                job_id=job.id,
                role="planner",
                instruction="own the goal",
                status=TaskStatus.COMPLETE,
                payload={"continuous_planner": True, "mode": "analysis"},
            )
            store.save_task(planner)
            finding = _finding(
                job.id,
                planner.id,
                {
                    "enqueue_subtasks": [
                        {"role": "implement", "instruction": "build the module"},
                    ]
                },
            )
            store.save_artifact(finding)
            created = store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=planner.id, created_by="worker-planner"
            )
            self.assertEqual(created, [])
            reasons = [
                str((event.get("payload") or {}).get("reason") or "")
                for event in _events(store, job.id, "task.enqueue_refused")
            ]
            self.assertIn(REASON_INTENT_SPEC, reasons)

    def test_planner_with_intent_can_fan_out_more_than_four(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("continuous")
            planner = Task(
                job_id=job.id,
                role="planner",
                instruction="own the goal",
                status=TaskStatus.COMPLETE,
                payload={"continuous_planner": True, "mode": "analysis"},
            )
            store.save_task(planner)
            intent = {
                "schema_version": 1,
                "kind": "intent_spec",
                "architecture": "one module per crate analog",
                "out_of_scope": ["rewrite the store"],
                "dependency_philosophy": "no new runtime deps",
                "resource_timeouts": "unittest under 30s",
                "fanout_min": 8,
                "fanout_max": 12,
                "decision": "lock intent",
                "why": "compute amplifies a vague spec",
            }
            store.save_artifact(_decision(job.id, planner.id, intent))
            self.assertIsNotNone(intent_spec_for_job(store.list_artifacts(job.id)))
            finding = _finding(
                job.id,
                planner.id,
                {
                    "enqueue_subtasks": [
                        {"role": "implement", "instruction": f"slice {index}"}
                        for index in range(8)
                    ]
                },
            )
            store.save_artifact(finding)
            created = store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=planner.id, created_by="worker-planner"
            )
            self.assertEqual(len(created), 8)
            self.assertLessEqual(len(created), DEFAULT_FANOUT_MAX)
            self.assertTrue(is_planner_task(planner))
            for child in created:
                store.save_task(replace(child, status=TaskStatus.COMPLETE))
            nxt = maybe_requeue_planner(store, replace(created[-1], status=TaskStatus.COMPLETE))
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.role, "planner")
            self.assertIn("Handoffs:", nxt.instruction)

    def test_requeues_planner_after_children_finish_not_on_planner_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("continuous")
            planner = Task(
                job_id=job.id,
                role="planner",
                instruction="own the goal",
                status=TaskStatus.COMPLETE,
                payload={"continuous_planner": True, "planner_iteration": 0, "mode": "analysis"},
            )
            store.save_task(planner)
            self.assertIsNone(maybe_requeue_planner(store, planner))
            worker = store.enqueue_subtask(
                job.id,
                parent_task_id=planner.id,
                role="implement",
                instruction="land slice",
                created_by="coordinator",
                actor="coordinator",
            )
            assert worker is not None
            store.save_task(replace(worker, status=TaskStatus.COMPLETE))
            store.save_artifact(
                _finding(
                    job.id,
                    worker.id,
                    {
                        "done": "slice landed",
                        "deviations": ["used existing helper"],
                        "concerns": ["needs snapshot"],
                    },
                )
            )
            nxt = maybe_requeue_planner(store, replace(worker, status=TaskStatus.COMPLETE))
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.role, "planner")
            self.assertEqual(nxt.payload.get("planner_iteration"), 1)
            self.assertIn("needs snapshot", nxt.instruction)
            again = maybe_requeue_planner(store, replace(worker, status=TaskStatus.COMPLETE))
            self.assertEqual(again.id, nxt.id)

    def test_scope_complete_stops_the_loop(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("continuous")
            planner = Task(
                job_id=job.id,
                role="planner",
                instruction="own the goal",
                status=TaskStatus.COMPLETE,
                payload={"continuous_planner": True, "mode": "analysis"},
            )
            store.save_task(planner)
            store.save_artifact(
                _decision(
                    job.id,
                    planner.id,
                    {"decision": "scope_complete", "why": "green snapshot passed", "kind": "scope_complete"},
                )
            )
            worker = store.enqueue_subtask(
                job.id,
                parent_task_id=planner.id,
                role="snapshot",
                instruction="fixup green branch",
                payload={"lane": "snapshot"},
                created_by="coordinator",
                actor="coordinator",
            )
            assert worker is not None
            store.save_task(replace(worker, status=TaskStatus.COMPLETE))
            self.assertIsNone(maybe_requeue_planner(store, replace(worker, status=TaskStatus.COMPLETE)))

    def test_complete_task_hits_intent_gate_and_requeue(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("continuous complete_task")
            planner = Task(
                job_id=job.id,
                role="planner",
                instruction="own the goal",
                status=TaskStatus.QUEUED,
                payload={"continuous_planner": True, "planner_iteration": 0, "mode": "analysis"},
            )
            store.save_task(planner)
            planner = store.claim_task(planner.id, "worker-planner")
            assert planner is not None
            refused = _finding(
                job.id,
                planner.id,
                {"enqueue_subtasks": [{"role": "implement", "instruction": "build the module"}]},
            )
            store.save_artifact(refused)
            run = AgentRun(
                job_id=job.id,
                task_id=planner.id,
                role=planner.role,
                worker_id="worker-planner",
                status=TaskStatus.COMPLETE,
                completed_at=now_iso(),
            )
            store.complete_task(planner, run, [refused], {"task_id": planner.id})
            reasons = [
                str((event.get("payload") or {}).get("reason") or "")
                for event in _events(store, job.id, "task.enqueue_refused")
            ]
            self.assertIn(REASON_INTENT_SPEC, reasons)
            self.assertEqual(
                [task.role for task in store.list_tasks(job.id) if task.id != planner.id],
                [],
            )

            store.save_task(
                replace(
                    store.get_task_by_id(planner.id),
                    status=TaskStatus.COMPLETE,
                    payload={
                        "continuous_planner": True,
                        "planner_iteration": 0,
                        "mode": "analysis",
                    },
                )
            )
            worker = store.enqueue_subtask(
                job.id,
                parent_task_id=planner.id,
                role="implement",
                instruction="land slice",
                created_by="coordinator",
                actor="coordinator",
            )
            assert worker is not None
            worker = store.claim_task(worker.id, "worker-child")
            assert worker is not None
            handoff = _finding(
                job.id,
                worker.id,
                {
                    "done": "slice landed",
                    "deviations": [],
                    "concerns": ["needs snapshot"],
                },
            )
            store.save_artifact(handoff)
            child_run = AgentRun(
                job_id=job.id,
                task_id=worker.id,
                role=worker.role,
                worker_id="worker-child",
                status=TaskStatus.COMPLETE,
                completed_at=now_iso(),
            )
            store.complete_task(worker, child_run, [handoff], {"task_id": worker.id})
            planners = [
                task
                for task in store.list_tasks(job.id)
                if task.role == "planner" and task.id != planner.id
            ]
            self.assertEqual(len(planners), 1)
            self.assertIn("needs snapshot", planners[0].instruction)
            self.assertEqual(planners[0].payload.get("planner_iteration"), 1)


class PromptContractTests(unittest.TestCase):
    def test_handoff_and_constraints_stay_before_instruction(self) -> None:
        analysis = structured_prompt_for_task(
            Task(job_id="j", role="explore", instruction="review auth"),
            prompt="review auth",
        )
        self.assertIn("deviations", analysis)
        self.assertIn("No TODOs, no partial implementations", analysis)
        self.assertLess(analysis.index("No TODOs"), analysis.index(TASK_INSTRUCTION_HEADER))
        implement = build_implement_prompt("fix the flaky test")
        self.assertIn("No TODOs, no partial implementations", implement)
        self.assertIn("deviations", implement)
        self.assertTrue(implement.rstrip().endswith("fix the flaky test"))

    def test_planner_contract_lives_in_the_task_body(self) -> None:
        prompt = structured_prompt_for_task(
            Task(
                job_id="j",
                role="planner",
                instruction="deliver the feature",
                payload={"continuous_planner": True},
            ),
            prompt="deliver the feature",
        )
        self.assertIn("Do not write code", prompt)
        self.assertIn("8-20", prompt)
        self.assertGreater(prompt.index("Do not write code"), prompt.index(TASK_INSTRUCTION_HEADER))
        explore = structured_prompt_for_task(
            Task(job_id="j", role="explore", instruction="review auth"),
            prompt="review auth",
        )
        self.assertNotIn("Do not write code", explore)
