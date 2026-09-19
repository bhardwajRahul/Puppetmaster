from __future__ import annotations

"""Hermetic Jev transition-oracle tests. Never hit OpenRouter."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
from unittest.mock import patch

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.jev import acting, decide_noul, enabled, opted_in
from puppetmaster.jev.client import decide, resolve_openrouter_key
from puppetmaster.jev.edges import (
    EDGE_ALREADY_ANSWERED,
    EDGE_CONFLICT_AUDITOR,
    apply_already_answered,
    apply_conflict_auditor_gate,
    decide_already_answered,
    decide_conflict_auditor,
    may_act,
    record_already_answered_on_job,
)
from puppetmaster.orchestrator import Orchestrator
from puppetmaster.swarm_launch import reuse_analysis_if_answered
from puppetmaster.jev.questions import PAIR_CAP
from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task, TaskStatus
from puppetmaster.gist_admission import (
    is_admitted_for_shared_context,
    maybe_admit_finding_as_gist,
)
from puppetmaster.quality import assess_run_quality
from puppetmaster.store import SwarmStore
from puppetmaster.worker_runtime import WorkerRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
DELTA_BUS = "puppetmaster/adapters/_delta_bus.py"

_JEV_ENV = (
    "PUPPETMASTER_JEV",
    "PUPPETMASTER_JEV_ACT",
    "PUPPETMASTER_OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY",
)


def _clear_jev_env() -> dict:
    prior = {}
    for name in _JEV_ENV:
        prior[name] = os.environ.pop(name, None)
    return prior


def _restore_jev_env(prior: dict) -> None:
    for name, value in prior.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _finding(
    store: SwarmStore,
    task: Task,
    claim: str,
    *,
    evidence=None,
    confidence: float = 0.9,
    created_by: str = "worker-explore",
) -> Artifact:
    artifact = Artifact(
        job_id=task.job_id,
        task_id=task.id,
        type=ArtifactType.FINDING,
        created_by=created_by,
        confidence=confidence,
        evidence=list(evidence or ["locus.py:1"]),
        payload={"claim": claim},
    )
    store.save_artifact(artifact)
    return artifact


def _job_with_auditor(
    store: SwarmStore,
    *,
    cwd: Optional[str] = None,
    finding_claims=None,
):
    job = store.create_job("score peer findings")
    store.update_job_status(job.id, JobStatus.RUNNING)
    explore = Task(
        job_id=job.id,
        role="explore",
        instruction="find facts",
        status=TaskStatus.COMPLETE,
        payload={"cwd": cwd or str(REPO_ROOT)},
    )
    auditor = Task(
        job_id=job.id,
        role="conflict-auditor",
        instruction="audit conflicts",
        status=TaskStatus.QUEUED,
        depends_on=[explore.id],
        payload={"cwd": cwd or str(REPO_ROOT)},
    )
    store.save_task(explore)
    store.save_task(auditor)
    claims = finding_claims
    if claims is None:
        claims = (
            "Explore found a stable helper spawn semaphore",
            "Explore found file-claim admission stays mechanical",
        )
    findings = [
        _finding(store, explore, claim, evidence=["workers.py:%d" % (10 + index)])
        for index, claim in enumerate(claims)
    ]
    return job, explore, auditor, findings


class JevOptInTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()

    def tearDown(self) -> None:
        _restore_jev_env(self._prior)

    def test_unset_is_not_opted_in_even_with_key(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        self.assertFalse(opted_in())
        self.assertFalse(enabled())

    def test_auto_and_off_stay_off(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        for raw in ("", "auto", "0", "off", "no"):
            if raw:
                os.environ["PUPPETMASTER_JEV"] = raw
            else:
                os.environ.pop("PUPPETMASTER_JEV", None)
            self.assertFalse(opted_in(), raw)

    def test_opt_in_without_key_is_not_enabled(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        self.assertTrue(opted_in())
        self.assertFalse(enabled())
        self.assertFalse(acting())

    def test_act_without_opt_in_is_false(self) -> None:
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        self.assertFalse(acting())

    def test_act_requires_opt_in(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        self.assertTrue(acting())

    def test_v1_may_act_on_opt_in_without_act_flag(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        self.assertTrue(may_act(EDGE_CONFLICT_AUDITOR))
        self.assertFalse(may_act(EDGE_ALREADY_ANSWERED))
        self.assertFalse(acting())

    def test_opt_in_with_puppetmaster_key_enables(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "true"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-pm"
        os.environ["OPENROUTER_API_KEY"] = "sk-generic"
        self.assertTrue(enabled())
        self.assertEqual(resolve_openrouter_key(), "sk-pm")

    def test_unset_never_calls_urlopen(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        with patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(decide_noul({"goal": "x"}, "Is this true?"))
            self.assertIsNone(decide({"goal": "x"}, {"q": {"type": "noul"}}))
            urlopen.assert_not_called()

    def test_opt_in_without_key_never_calls_urlopen(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        with patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(decide_noul({"goal": "x"}, "Is this true?"))
            urlopen.assert_not_called()


class JevClientFailOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"

    def tearDown(self) -> None:
        _restore_jev_env(self._prior)

    def test_urlopen_raise_returns_none(self) -> None:
        def _boom(_req, timeout=None):
            raise OSError("no route")

        with patch("urllib.request.urlopen", _boom):
            self.assertIsNone(decide({"goal": "x"}, {"q": {"type": "noul"}}))

    def test_malformed_body_returns_none(self) -> None:
        class _Resp:
            def read(self):
                return b'{"not": "answers"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("urllib.request.urlopen", return_value=_Resp()):
            self.assertIsNone(decide({"goal": "x"}, {"q": {"type": "noul"}}))


class ConflictAuditorGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        self._tmp = TemporaryDirectory()
        self.store = SwarmStore(Path(self._tmp.name) / ".puppetmaster")
        self.store.init()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        _restore_jev_env(self._prior)

    def _gates(self, job_id: str):
        return [
            artifact
            for artifact in self.store.list_artifacts(job_id)
            if artifact.type == ArtifactType.GATE
            and (artifact.payload or {}).get("gate") == "jev_transition"
        ]

    def test_unset_does_not_network_and_leaves_auditor_queued(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)
        with patch("urllib.request.urlopen") as urlopen:
            decision = apply_conflict_auditor_gate(self.store, auditor)
            urlopen.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "not_opted_in")
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)
        self.assertEqual(self._gates(auditor.job_id), [])

    def test_opt_in_no_key_fail_open_no_socket(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)
        with patch("urllib.request.urlopen") as urlopen:
            decision = apply_conflict_auditor_gate(self.store, auditor)
            urlopen.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "fail_open")
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)
        gates = self._gates(auditor.job_id)
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].payload["action"], "spawn")
        self.assertEqual(gates[0].payload["would_action"], "spawn")
        self.assertFalse(gates[0].payload["acted"])
        self.assertTrue(gates[0].payload["passed"])

    def test_opt_in_low_noul_skips_auditor(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)

        def _fake_decide(state, questions, key=""):
            return {
                "answers": {
                    name: {"noul": 0.12} for name in questions
                }
            }

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            decision = apply_conflict_auditor_gate(self.store, auditor)
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.would_action, "skip")
        self.assertTrue(decision.acted)
        self.assertEqual(decision.reason, "no_pair_above_threshold")
        self.assertEqual(decision.max_noul, 0.12)
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.SKIPPED)
        gates = self._gates(auditor.job_id)
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].payload["action"], "skip")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertTrue(gates[0].payload["acted"])
        self.assertTrue(gates[0].payload["passed"])
        verifications = [
            artifact
            for artifact in self.store.list_artifacts(auditor.job_id)
            if artifact.type == ArtifactType.VERIFICATION
            and (artifact.payload or {}).get("check")
            == "jev_transition:conflict_auditor"
        ]
        self.assertEqual(verifications[0].payload["result"], "skipped")

    def test_act_compatible_low_noul_skips_auditor(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)

        def _fake_decide(state, questions, key=""):
            return {
                "answers": {
                    name: {"noul": 0.12} for name in questions
                }
            }

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            decision = apply_conflict_auditor_gate(self.store, auditor)
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.would_action, "skip")
        self.assertTrue(decision.acted)
        self.assertEqual(decision.reason, "no_pair_above_threshold")
        self.assertEqual(decision.max_noul, 0.12)
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.SKIPPED)
        gates = self._gates(auditor.job_id)
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].payload["action"], "skip")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertTrue(gates[0].payload["acted"])
        self.assertTrue(gates[0].payload["passed"])
        verifications = [
            artifact
            for artifact in self.store.list_artifacts(auditor.job_id)
            if artifact.type == ArtifactType.VERIFICATION
        ]
        self.assertEqual(verifications[0].payload["result"], "skipped")
        self.assertEqual(
            verifications[0].payload["check"], "jev_transition:conflict_auditor"
        )

    def test_high_noul_keeps_auditor_queued(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)

        def _fake_decide(state, questions, key=""):
            return {"answers": {name: {"noul": 0.71} for name in questions}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            decision = apply_conflict_auditor_gate(self.store, auditor)
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "noul_above_threshold")
        self.assertEqual(decision.max_noul, 0.71)
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)

    def test_mechanical_conflict_skips_jev(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        claims = (
            "%s protects sinks with asyncio.Lock" % DELTA_BUS,
            "%s protects sinks with threading.Lock" % DELTA_BUS,
        )
        _job, explore, auditor, _findings = _job_with_auditor(
            self.store, finding_claims=()
        )
        _finding(
            self.store,
            explore,
            claims[0],
            evidence=["%s:22" % DELTA_BUS],
        )
        _finding(
            self.store,
            explore,
            claims[1],
            evidence=["%s:22" % DELTA_BUS],
        )
        with patch("puppetmaster.jev.edges.decide") as mocked:
            decision = apply_conflict_auditor_gate(self.store, auditor)
            mocked.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "mechanical_conflict")
        self.assertTrue(str(decision.evidence[0]).startswith("mechanical:"))
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)

    def test_urlopen_raise_keeps_auditor_queued(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)

        def _boom(*args, **kwargs):
            raise OSError("timeout")

        with patch("puppetmaster.jev.client.urllib.request.urlopen", _boom):
            decision = apply_conflict_auditor_gate(self.store, auditor)
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "fail_open")
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)

    def test_one_finding_does_not_skip(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(
            self.store, finding_claims=("Only one repository fact",)
        )
        with patch("puppetmaster.jev.edges.decide") as mocked:
            decision = apply_conflict_auditor_gate(self.store, auditor)
            mocked.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "thin_set")
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)

    def test_pair_cap_fail_open_does_not_score_subset(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        count = 0
        needed = 0
        while needed <= PAIR_CAP:
            count += 1
            needed = count * (count - 1) // 2
        claims = ["Compatible fact %d about routing" % i for i in range(count)]
        _job, _explore, auditor, _findings = _job_with_auditor(
            self.store, finding_claims=claims
        )
        with patch("puppetmaster.jev.edges.decide") as mocked:
            decision = apply_conflict_auditor_gate(self.store, auditor)
            mocked.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "pair_cap")
        self.assertTrue(decision.pairs_capped)
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)

    def test_quality_ok_after_skip_with_explore_finding(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, findings = _job_with_auditor(self.store)

        def _fake_decide(state, questions, key=""):
            return {"answers": {name: {"noul": 0.12} for name in questions}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            apply_conflict_auditor_gate(self.store, auditor)
        verdict = assess_run_quality(self.store.list_artifacts(auditor.job_id))
        self.assertEqual(verdict["quality"], "ok")
        self.assertTrue(verdict["trustworthy"])
        self.assertGreaterEqual(len(findings), 2)

    def test_worker_runtime_skip_does_not_construct_local_worker(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)

        def _fake_decide(state, questions, key=""):
            return {"answers": {name: {"noul": 0.08} for name in questions}}

        class _BoomWorker:
            def __init__(self, role, worker_id=None):
                raise AssertionError("LocalWorker must not spawn on Jev skip")

        runtime = WorkerRuntime(
            store=self.store,
            job_id=auditor.job_id,
            role="conflict-auditor",
            worker_id="w-auditor",
        )
        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            with patch("puppetmaster.worker_runtime.LocalWorker", _BoomWorker):
                self.assertTrue(runtime.run_once())
        stored = self.store.get_task_by_id(auditor.id)
        self.assertEqual(stored.status, TaskStatus.SKIPPED)


class PersistNotOptedInTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        self._tmp = TemporaryDirectory()
        self.store = SwarmStore(Path(self._tmp.name) / ".puppetmaster")
        self.store.init()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        _restore_jev_env(self._prior)

    def test_not_opted_in_gate_only_when_requested(self) -> None:
        _job, _explore, auditor, _findings = _job_with_auditor(self.store)
        decision = decide_conflict_auditor(
            self.store, auditor.job_id, persist_not_opted_in=True
        )
        self.assertEqual(decision.reason, "not_opted_in")
        self.assertTrue(decision.write_gate)
        apply_conflict_auditor_gate(
            self.store, auditor, persist_not_opted_in=True
        )
        gates = [
            artifact
            for artifact in self.store.list_artifacts(auditor.job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["reason"], "not_opted_in")


def _prior_analysis_job(store: SwarmStore, cwd: str, claim: str) -> Task:
    job = store.create_job("already known analysis")
    store.update_job_status(job.id, JobStatus.RUNNING)
    task = Task(
        job_id=job.id,
        role="explore",
        instruction="prior facts",
        status=TaskStatus.COMPLETE,
        payload={"cwd": cwd},
    )
    store.save_task(task)
    _finding(store, task, claim, evidence=["orchestrator.py:10"])
    store.update_job_status(job.id, JobStatus.COMPLETE)
    return task


class AlreadyAnsweredTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        self._tmp = TemporaryDirectory()
        self.store = SwarmStore(Path(self._tmp.name) / ".puppetmaster")
        self.store.init()
        self.cwd = str(REPO_ROOT)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        _restore_jev_env(self._prior)

    def test_unset_does_not_network_and_launches(self) -> None:
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        _prior_analysis_job(self.store, self.cwd, "Prior FINDING about spawn skip")
        with patch("urllib.request.urlopen") as urlopen:
            decision = decide_already_answered(
                self.store, "audit spawn skip again", self.cwd
            )
            urlopen.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertIsNone(apply_already_answered(self.store, "audit spawn skip again", self.cwd))

    def test_observe_high_noul_does_not_reuse(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        prior = _prior_analysis_job(
            self.store, self.cwd, "Explore found the auditor skip path"
        )
        jobs_before = {job.id for job in self.store.list_jobs()}

        def _fake_decide(state, questions, key=""):
            return {"answers": {"already_answered": {"noul": 0.81}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            reused = apply_already_answered(
                self.store, "how does auditor skip work", self.cwd
            )
            receipt = reuse_analysis_if_answered(
                self.store, "how does auditor skip work", self.cwd
            )
        self.assertIsNone(reused)
        self.assertIsNone(receipt)
        self.assertEqual({job.id for job in self.store.list_jobs()}, jobs_before)
        self.assertEqual(
            [
                artifact
                for artifact in self.store.list_artifacts(prior.job_id)
                if artifact.type == ArtifactType.GATE
            ],
            [],
        )

        launched = self.store.create_job("how does auditor skip work")
        launched_task = Task(
            job_id=launched.id,
            role="explore",
            instruction="today",
            status=TaskStatus.QUEUED,
            payload={"cwd": self.cwd},
        )
        self.store.save_task(launched_task)
        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            bound = record_already_answered_on_job(
                self.store,
                launched.id,
                "how does auditor skip work",
                self.cwd,
                task_id=launched_task.id,
            )
        self.assertIsNotNone(bound)
        self.assertEqual(bound.action, "spawn")
        self.assertEqual(bound.would_action, "skip")
        self.assertFalse(bound.acted)
        gates = [
            artifact
            for artifact in self.store.list_artifacts(launched.id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "already_answered")
        self.assertEqual(gates[0].payload["action"], "spawn")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertFalse(gates[0].payload["acted"])
        self.assertEqual(gates[0].payload["max_noul"], 0.81)
        self.assertEqual(gates[0].evidence[0], prior.job_id)

    def test_orchestrator_observe_stamps_launched_analysis_job(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        prior = _prior_analysis_job(
            self.store, self.cwd, "Explore found the auditor skip path"
        )
        launched = self.store.create_job("how does auditor skip work")
        launched_task = Task(
            job_id=launched.id,
            role="explore",
            instruction="today",
            status=TaskStatus.QUEUED,
            payload={"cwd": self.cwd},
        )
        self.store.save_task(launched_task)
        from puppetmaster.workers import WorkerSpec

        spec = WorkerSpec(
            role="explore",
            instruction="today",
            adapter="local",
            payload={"cwd": self.cwd, "mode": "analysis"},
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"already_answered": {"noul": 0.81}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            Orchestrator(self.store)._maybe_record_already_answered(
                launched, "how does auditor skip work", [spec], [launched_task]
            )
        gates = [
            artifact
            for artifact in self.store.list_artifacts(launched.id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertFalse(gates[0].payload["acted"])
        self.assertEqual(gates[0].evidence[0], prior.job_id)

    def test_orchestrator_skips_already_answered_on_edit_swarm(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _prior_analysis_job(
            self.store, self.cwd, "Explore found the auditor skip path"
        )
        launched = self.store.create_job("implement the skip path")
        launched_task = Task(
            job_id=launched.id,
            role="implement",
            instruction="edit",
            status=TaskStatus.QUEUED,
            payload={"cwd": self.cwd, "mode": "implement"},
        )
        self.store.save_task(launched_task)
        from puppetmaster.workers import WorkerSpec

        spec = WorkerSpec(
            role="implement",
            instruction="edit",
            adapter="local",
            payload={"cwd": self.cwd, "mode": "implement"},
        )
        with patch("puppetmaster.jev.edges.decide") as mocked:
            Orchestrator(self.store)._maybe_record_already_answered(
                launched, "implement the skip path", [spec], [launched_task]
            )
            mocked.assert_not_called()
        self.assertEqual(
            [
                artifact
                for artifact in self.store.list_artifacts(launched.id)
                if artifact.type == ArtifactType.GATE
            ],
            [],
        )

    def test_act_high_noul_reuses_prior_and_writes_handle_gate(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        prior = _prior_analysis_job(
            self.store, self.cwd, "Explore found the auditor skip path"
        )
        jobs_before = {job.id for job in self.store.list_jobs()}

        def _fake_decide(state, questions, key=""):
            return {"answers": {"already_answered": {"noul": 0.81}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            reused = apply_already_answered(
                self.store, "how does auditor skip work", self.cwd
            )
            receipt = reuse_analysis_if_answered(
                self.store, "how does auditor skip work", self.cwd
            )
        self.assertIsNotNone(reused)
        self.assertEqual(reused.prior_job_id, prior.job_id)
        self.assertNotIn(reused.handle_job_id, jobs_before)
        queued_workers = [
            task
            for task in self.store.list_tasks(reused.handle_job_id)
            if task.status == TaskStatus.QUEUED
        ]
        self.assertEqual(queued_workers, [])
        gates = [
            artifact
            for artifact in self.store.list_artifacts(reused.handle_job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "already_answered")
        self.assertEqual(gates[0].payload["action"], "skip")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertTrue(gates[0].payload["acted"])
        self.assertTrue(gates[0].payload["passed"])
        self.assertEqual(receipt["job_id"], prior.job_id)
        self.assertTrue(receipt["reused"])

    def test_fail_open_launches(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _prior_analysis_job(self.store, self.cwd, "Prior FINDING about spawn skip")

        def _boom(*args, **kwargs):
            raise OSError("timeout")

        with patch("puppetmaster.jev.edges.decide", _boom):
            self.assertIsNone(
                apply_already_answered(self.store, "audit spawn skip again", self.cwd)
            )

    def test_explicit_trigger_still_launches(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        _prior_analysis_job(self.store, self.cwd, "Prior FINDING about spawn skip")
        with patch("puppetmaster.jev.edges.decide") as mocked:
            decision = decide_already_answered(
                self.store, "Use Puppetmaster to audit spawn skip again", self.cwd
            )
            mocked.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "explicit_trigger")

    def test_empty_prior_does_not_call_jev(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        with patch("puppetmaster.jev.edges.decide") as mocked:
            decision = decide_already_answered(
                self.store, "brand new audit", self.cwd
            )
            mocked.assert_not_called()
        self.assertEqual(decision.action, "spawn")
        self.assertEqual(decision.reason, "thin_set")


def _supported_finding(store: SwarmStore, claim: str) -> Artifact:
    job = store.create_job("admit overlay")
    task = Task(
        job_id=job.id,
        role="explore",
        instruction="find",
        status=TaskStatus.COMPLETE,
        payload={"cwd": str(REPO_ROOT)},
    )
    store.save_task(task)
    finding = _finding(store, task, claim, evidence=["gist_admission.py:1"])
    store.save_artifact(
        Artifact(
            job_id=job.id,
            task_id=task.id,
            type=ArtifactType.VERIFICATION,
            created_by="worker-explore",
            confidence=0.4,
            evidence=[finding.id],
            payload={"check": claim, "result": "passed"},
        )
    )
    return finding


class FindingAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        self._tmp = TemporaryDirectory()
        self.store = SwarmStore(Path(self._tmp.name) / ".puppetmaster")
        self.store.init()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        _restore_jev_env(self._prior)

    def test_unset_admits_without_network(self) -> None:
        finding = _supported_finding(
            self.store, "WorkerRuntime skips conflict-auditor before LocalWorker"
        )
        with patch("urllib.request.urlopen") as urlopen:
            gist = maybe_admit_finding_as_gist(self.store, finding)
            urlopen.assert_not_called()
        self.assertIsNotNone(gist)
        self.assertTrue(is_admitted_for_shared_context(finding))

    def test_observe_low_noul_still_admits(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _supported_finding(
            self.store, "Return only Puppetmaster artifact JSON with an artifacts array"
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"repo_fact": {"noul": 0.12}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            gist = maybe_admit_finding_as_gist(self.store, finding)
        self.assertIsNotNone(gist)
        loaded = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.id == finding.id
        ][0]
        self.assertTrue(is_admitted_for_shared_context(loaded))
        gates = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "finding_admission")
        self.assertEqual(gates[0].payload["action"], "spawn")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertFalse(gates[0].payload["acted"])
        self.assertEqual(gates[0].payload["max_noul"], 0.12)

    def test_act_low_noul_demotes_and_blocks_injection(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _supported_finding(
            self.store, "Return only Puppetmaster artifact JSON with an artifacts array"
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"repo_fact": {"noul": 0.12}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            self.assertIsNone(maybe_admit_finding_as_gist(self.store, finding))
        loaded = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.id == finding.id
        ][0]
        self.assertFalse(is_admitted_for_shared_context(loaded))
        self.assertFalse(
            any(
                artifact.type == ArtifactType.GIST
                for artifact in self.store.list_artifacts(finding.job_id)
            )
        )
        gates = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "finding_admission")
        self.assertEqual(gates[0].payload["action"], "skip")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertTrue(gates[0].payload["acted"])

    def test_high_noul_keeps_today_admission(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _supported_finding(
            self.store, "detect_contradictory_peers flags lock-type peers"
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"repo_fact": {"noul": 0.71}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            gist = maybe_admit_finding_as_gist(self.store, finding)
        self.assertIsNotNone(gist)

    def test_jev_failure_fail_opens_to_today(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _supported_finding(
            self.store, "SATISFIED_TASK_STATUSES includes SKIPPED"
        )

        def _boom(*args, **kwargs):
            raise OSError("timeout")

        with patch("puppetmaster.jev.edges.decide", _boom):
            gist = maybe_admit_finding_as_gist(self.store, finding)
        self.assertIsNotNone(gist)


def _enqueue_fixture(store: SwarmStore, claim: str) -> Artifact:
    job = store.create_job("continue the goal")
    store.update_job_status(job.id, JobStatus.RUNNING)
    parent = Task(
        job_id=job.id,
        role="explore",
        instruction="wave one",
        status=TaskStatus.COMPLETE,
        payload={"cwd": str(REPO_ROOT)},
    )
    store.save_task(parent)
    finding = Artifact(
        job_id=job.id,
        task_id=parent.id,
        type=ArtifactType.FINDING,
        created_by="worker-explore",
        confidence=0.9,
        evidence=["continuous_plan.py:1"],
        payload={
            "claim": claim,
            "enqueue_subtasks": [
                {"role": "explore", "instruction": "look again"},
            ],
        },
    )
    store.save_artifact(finding)
    return finding


class StopSpawnTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = _clear_jev_env()
        self._tmp = TemporaryDirectory()
        self.store = SwarmStore(Path(self._tmp.name) / ".puppetmaster")
        self.store.init()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        _restore_jev_env(self._prior)

    def test_unset_still_enqueues_without_network(self) -> None:
        finding = _enqueue_fixture(
            self.store, "The skip path lives in WorkerRuntime.run_once"
        )
        with patch("urllib.request.urlopen") as urlopen:
            created = self.store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=finding.task_id, created_by="w"
            )
            urlopen.assert_not_called()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].role, "explore")

    def test_observe_low_unanswered_noul_still_enqueues(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _enqueue_fixture(
            self.store, "The skip path lives in WorkerRuntime.run_once"
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"still_unanswered": {"noul": 0.18}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            created = self.store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=finding.task_id, created_by="w"
            )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].role, "explore")
        gates = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "stop_spawn")
        self.assertEqual(gates[0].payload["action"], "spawn")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertFalse(gates[0].payload["acted"])
        self.assertEqual(gates[0].payload["max_noul"], 0.18)

    def test_act_low_unanswered_noul_stops_follow_up(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_JEV_ACT"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _enqueue_fixture(
            self.store, "The skip path lives in WorkerRuntime.run_once"
        )

        def _fake_decide(state, questions, key=""):
            return {"answers": {"still_unanswered": {"noul": 0.18}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            created = self.store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=finding.task_id, created_by="w"
            )
        self.assertEqual(created, [])
        extras = [
            task
            for task in self.store.list_tasks(finding.job_id)
            if task.role == "explore" and task.id != finding.task_id
        ]
        self.assertEqual(extras, [])
        gates = [
            artifact
            for artifact in self.store.list_artifacts(finding.job_id)
            if artifact.type == ArtifactType.GATE
        ]
        self.assertEqual(gates[0].payload["edge"], "stop_spawn")
        self.assertEqual(gates[0].payload["action"], "skip")
        self.assertEqual(gates[0].payload["would_action"], "skip")
        self.assertTrue(gates[0].payload["acted"])

    def test_high_unanswered_noul_still_enqueues(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _enqueue_fixture(self.store, "Only one locus was checked")

        def _fake_decide(state, questions, key=""):
            return {"answers": {"still_unanswered": {"noul": 0.84}}}

        with patch("puppetmaster.jev.edges.decide", _fake_decide):
            created = self.store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=finding.task_id, created_by="w"
            )
        self.assertEqual(len(created), 1)

    def test_jev_failure_fail_opens_enqueue(self) -> None:
        os.environ["PUPPETMASTER_JEV"] = "1"
        os.environ["PUPPETMASTER_OPENROUTER_API_KEY"] = "sk-test"
        finding = _enqueue_fixture(self.store, "Only one locus was checked")

        def _boom(*args, **kwargs):
            raise OSError("timeout")

        with patch("puppetmaster.jev.edges.decide", _boom):
            created = self.store.maybe_enqueue_follow_ups_from_artifact(
                finding, parent_task_id=finding.task_id, created_by="w"
            )
        self.assertEqual(len(created), 1)
