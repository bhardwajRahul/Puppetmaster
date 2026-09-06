"""Consumption reporting is independent of selected artifacts and retry state."""
from dataclasses import asdict, replace
import unittest
from unittest.mock import patch

from test_attempt_ledger import LedgerFixture
from test_cost_report import _usage
from puppetmaster.attempts import UsageObservation
from puppetmaster.consumption import build_attempt_consumption_report
from puppetmaster.cost import build_cost_report, maybe_stamp_terminal_cost_receipt
from puppetmaster.dashboard import build_job_snapshot
from puppetmaster.models import JobStatus, TaskStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore
from puppetmaster.usage import aggregate_token_usage, select_usage_records


class ConsumptionContract(LedgerFixture):
    def report(self):
        return build_attempt_consumption_report(self.store, self.job.id)

    def capture(self, attempt, key="usage", **values):
        self.store.record_attempt(attempt)
        observation = UsageObservation(self.job.id, attempt.attempt_id, key,
                                       "test", "2026-09-06", **values)
        self.store.record_usage_observation(observation)
        return observation

    def test_failed_and_successful_attempts_survive_reset_and_reopen(self):
        self.capture(self.attempt, usage_state="measured", tokens_in=12,
                     cost_basis="api", cost_state="measured", cost_usd=.25)
        self.store.save_run(replace(self.run, status=TaskStatus.FAILED))
        self.store.save_task(replace(self.task, status=TaskStatus.FAILED, attempts=9))
        self.store.reset_subgraph(self.job.id, [self.task.id])
        self.capture(replace(self.attempt, attempt_id="retry"),
                     usage_state="measured", tokens_in=30,
                     cost_basis="api", cost_state="measured", cost_usd=.5)
        self.store.save_run(replace(self.run, status=TaskStatus.COMPLETE))
        report = self.report()
        self.assertEqual(report.totals.tokens_in.total, 42)
        self.assertEqual(report.totals.api_cost_usd.total, .75)
        self.assertEqual(report.attempt_count, 2)
        self.assertEqual(self.store.get_task_by_id(self.task.id).attempts, 0)
        self.store = self.store_type(self.root)
        self.assertEqual(self.report(), report)

    def test_snapshots_dedupe_and_fill_missing_metrics(self):
        obs = self.capture(self.attempt, usage_state="measured", tokens_in=12)
        self.assertFalse(self.store.record_usage_observation(obs))
        self.store.record_usage_observation(replace(obs, observation_id="recapture"))
        self.capture(self.attempt, "more", usage_state="measured", tokens_in=12,
                     tokens_out=4)
        self.capture(self.attempt, "unknown")
        report = self.report()
        self.assertEqual(report.totals.tokens_in.total, 12)
        self.assertEqual(report.totals.tokens_out.total, 4)
        self.assertEqual(len(report.attempts[0].observation_ids), 4)

    def test_unknown_partial_and_conflicting_snapshots(self):
        self.capture(self.attempt, usage_state="measured", tokens_in=12)
        self.capture(replace(self.attempt, attempt_id="unknown"))
        metric = self.report().totals.tokens_in
        self.assertEqual(metric.known_subtotal, 12)
        self.assertIsNone(metric.total)
        self.assertEqual(metric.status, "partial")
        self.assertEqual(metric.unknown_attempts, 1)
        self.assertIsNone(self.report().totals.tokens_out.total)
        self.capture(self.attempt, "conflict", usage_state="measured", tokens_in=15)
        metric = self.report().totals.tokens_in
        self.assertIsNone(metric.total)
        self.assertEqual(metric.conflicting_attempts, 1)

    def test_start_without_observation_is_unknown(self):
        self.store.record_attempt(self.attempt)
        report = self.report()
        self.assertEqual(report.attempt_count, 1)
        self.assertEqual(report.attempts[0].observation_ids, ())
        self.assertEqual(report.attempts[0].process_outcomes, ())
        self.assertEqual(report.totals.tokens_in.unknown_attempts, 1)
        self.assertIsNone(report.totals.tokens_in.total)

    def test_estimate_provenance_and_conflicting_cost(self):
        self.capture(self.attempt, usage_state="estimated", tokens_in=10,
                     cost_basis="api", cost_state="estimated", cost_usd=1)
        self.assertEqual(self.report().totals.tokens_in.status, "estimated")
        self.capture(self.attempt, "measured", usage_state="measured", tokens_in=10,
                     cost_basis="api", cost_state="measured", cost_usd=1)
        self.assertEqual(self.report().totals.tokens_in.status, "measured")
        self.capture(self.attempt, "conflicting", cost_basis="api",
                     cost_state="measured", cost_usd=2)
        self.assertEqual(self.report().totals.api_cost_usd.conflicting_attempts, 1)
        self.assertIsNone(self.report().totals.api_cost_usd.total)

    def test_plan_api_estimates_and_measured_zero(self):
        self.capture(self.attempt, usage_state="measured", tokens_in=0, tokens_out=0,
                     cost_basis="plan_marginal", cost_state="measured", cost_usd=0)
        self.capture(self.attempt, "equivalent", cost_basis="api_equivalent",
                     cost_state="estimated", cost_usd=2)
        totals = self.report().totals
        self.assertEqual(totals.tokens_in.total, 0)
        self.assertEqual(totals.tokens_in.status, "measured")
        self.assertEqual(totals.plan_marginal_cost_usd.total, 0)
        self.assertEqual(totals.api_equivalent_cost_usd.total, 2)
        self.assertEqual(totals.api_equivalent_cost_usd.status, "estimated")
        self.assertIsNone(totals.api_cost_usd.total)
        self.capture(replace(self.attempt, attempt_id="api"), cost_basis="api",
                     cost_state="measured", cost_usd=3)
        totals = self.report().totals
        self.assertEqual(totals.api_cost_usd.known_subtotal, 3)
        self.assertIsNone(totals.api_cost_usd.total)
        self.assertEqual(totals.api_equivalent_cost_usd.known_subtotal, 2)

    def test_empty_legacy_reuse_and_deletion_are_unknown(self):
        self.store.save_run(self.run)
        self.assertEqual(self.report().attempt_count, 0)
        self.assertIsNone(self.report().totals.tokens_in.total)
        self.capture(self.attempt, usage_state="measured", tokens_in=0)
        self.store.delete_job(self.job.id)
        self.assertEqual(self.report().attempts, ())
        self.assertIsNone(self.report().totals.api_cost_usd.total)

    def test_deterministic_order_and_serialization(self):
        for identity in ("z", "a", "m"):
            self.capture(replace(self.attempt, attempt_id=identity), "z",
                         usage_state="measured", tokens_in=1)
            self.capture(replace(self.attempt, attempt_id=identity), "a")
        report = self.report()
        self.assertEqual([a.attempt.attempt_id for a in report.attempts], ["a", "m", "z"])
        self.assertEqual(report.attempts[0].observation_ids, ("a", "z"))
        self.assertEqual(report.to_dict(), asdict(report))
        attempts = self.store.list_attempts(self.job.id)
        observations = self.store.list_usage_observations(self.job.id)
        with patch.object(self.store, "list_attempts", return_value=attempts[::-1]), \
                patch.object(self.store, "list_usage_observations", return_value=observations[::-1]):
            self.assertEqual(self.report(), report)
        other_type = SQLiteSwarmStore if self.store_type is SwarmStore else SwarmStore
        other = other_type(self.root.parent / "other")
        other.init()
        other.save_job(self.job)
        other.save_task(self.task)
        for attempt in reversed(attempts):
            other.record_attempt(attempt)
        for observation in reversed(observations):
            other.record_usage_observation(observation)
        self.assertEqual(build_attempt_consumption_report(other, self.job.id), report)

    def test_selected_cost_receipt_dashboard_and_withdrawal(self):
        artifact = _usage(self.job.id, self.task.id, tokens_in=5, tokens_out=2,
                          real_cost_usd=.1)
        self.store.save_artifact(artifact)
        with patch("puppetmaster.cost.load_registry", return_value=[]):
            self.store.save_job(maybe_stamp_terminal_cost_receipt(
                self.store, replace(self.job, status=JobStatus.COMPLETE)))
            receipt = self.store.get_job(self.job.id).cost_receipt
            self.assertIsNotNone(receipt)
            baseline = build_cost_report(self.store, self.job.id)
            selected = select_usage_records(self.store.list_artifacts(self.job.id))
            aggregate = aggregate_token_usage(self.store.list_artifacts(self.job.id))
            self.capture(self.attempt, usage_state="measured", tokens_in=100,
                         cost_basis="api", cost_state="measured", cost_usd=3)
            consumption = self.report()
            snapshot = build_job_snapshot(self.store, self.job.id)
            self.assertEqual(snapshot["attempt_consumption"], consumption.to_dict())
            self.assertEqual(snapshot["cost"], baseline)
            self.assertEqual(build_cost_report(self.store, self.job.id), baseline)
            self.assertEqual(select_usage_records(self.store.list_artifacts(self.job.id)), selected)
            self.assertEqual(aggregate_token_usage(self.store.list_artifacts(self.job.id)), aggregate)
            self.store.save_artifact(replace(artifact, payload={
                **artifact.payload, "validation": {"status": "superseded"}}))
            self.assertEqual(select_usage_records(self.store.list_artifacts(self.job.id)), {})
            self.assertEqual(self.report(), consumption)
            self.assertEqual(self.store.get_job(self.job.id).cost_receipt, receipt)
            self.assertEqual(build_cost_report(self.store, self.job.id), baseline)


class FileConsumptionTests(ConsumptionContract, unittest.TestCase):
    pass


class SQLiteConsumptionTests(ConsumptionContract, unittest.TestCase):
    store_type = SQLiteSwarmStore
