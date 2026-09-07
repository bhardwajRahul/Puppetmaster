"""Final review regressions for metadata contention, effect dispatch, and wire types."""
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
import test_store_contracts
from puppetmaster.attempts import ExecutionAttempt, UsageObservation
from puppetmaster.contracts import ContractConflict, EffectReceipt, immutable_digest
from puppetmaster.models import to_jsonable
from puppetmaster.store_contracts import task_binding


class FinalReviewRepairTests(unittest.TestCase):
    stores = test_store_contracts.StoreContractTests.stores

    def test_real_metadata_lock_preserves_cursor_and_retry(self):
        for store, job, task, run, ref in self.stores():
            with self.subTest(backend=store.backend_name):
                store.create_job("second page")
                first = store.list_job_summaries(limit=1)
                self.assertIsNotNone(first.next_cursor)
                name = "state.sqlite3" if store.backend_name == "sqlite" else "metadata.sqlite3"
                blocker = sqlite3.connect(str(store.root / name))
                try:
                    # An exclusive rollback-journal writer blocks real read-only connections.
                    blocker.execute("PRAGMA journal_mode=DELETE")
                    blocker.execute("BEGIN EXCLUSIVE")
                    page = store.list_job_summaries(cursor=first.next_cursor, limit=1)
                    self.assertEqual(page.outcome, "unavailable")
                    self.assertEqual((page.items, page.revision, page.scanned), ((), 0, 0))
                    self.assertEqual(page.next_cursor, first.next_cursor)
                    with self.assertRaises(ValueError):
                        store.list_job_summaries(limit=0)
                finally:
                    blocker.rollback()
                    blocker.close()
                retried = store.list_job_summaries(cursor=page.next_cursor, limit=1)
                self.assertTrue(retried.items)
                self.assertNotEqual(retried.items[0].id, first.items[0].id)
                self.assertNotEqual(retried.outcome, "unavailable")

    def test_metadata_lock_codes_and_other_errors(self):
        for store, job, task, run, ref in self.stores():
            queries = (store.list_job_summaries, store.read_job_summary_changes,
                       lambda: store.list_task_refs(ref), lambda: store.list_artifact_refs(ref))
            errors = [sqlite3.OperationalError(message) for message in
                      ("database is locked", "database table is locked", "database schema is locked",
                       "database table is locked: projection_current", "database schema is locked: main")]
            for code in (5, 6, 5 | (2 << 8), 6 | (1 << 8)):
                error = sqlite3.OperationalError("extended lock")
                error.sqlite_errorcode = code
                errors.append(error)
            for query in queries:
                for error in errors:
                    with patch("puppetmaster.projections.connection", side_effect=error):
                        self.assertEqual(query().outcome, "unavailable")
                for error in (sqlite3.DatabaseError("database disk image is malformed"),
                              sqlite3.OperationalError("no such column: invalid"),
                              sqlite3.OperationalError("near SELECT: syntax error")):
                    with patch("puppetmaster.projections.connection", side_effect=error):
                        with self.assertRaises(type(error)):
                            query()

    def test_manual_effect_dispatch_checks_cancellation_but_allows_observations(self):
        for store, job, task, run, ref in self.stores():
            attempt = ExecutionAttempt.from_run(run, adapter="local")
            store.record_attempt(attempt)
            binding = task_binding(task)
            intent = EffectReceipt(ref, "blocked", immutable_digest({}), binding, run.id,
                                   attempt.attempt_id, 1, "not_dispatched", "safe")
            store.record_effect(intent)
            for outcome in ("succeeded", "failed_no_effect", "unknown"):
                flying = replace(intent, effect_id=outcome)
                store.record_effect(flying)
                store.advance_effect(ref, outcome, expected_revision=1, outcome="in_flight",
                                     evidence_refs=("dispatch",))
            store.request_cancellation(ref, "cancel", [binding])
            with self.assertRaisesRegex(ContractConflict, "scoped cancellation"):
                store.advance_effect(ref, "blocked", expected_revision=1, outcome="in_flight",
                                     evidence_refs=("dispatch",))
            self.assertEqual(store.get_effect_receipt(ref, "blocked"), intent)
            for outcome in ("succeeded", "failed_no_effect", "unknown"):
                replay = store.advance_effect(ref, outcome, expected_revision=1,
                                              outcome="in_flight", evidence_refs=("dispatch",))
                self.assertEqual(replay.revision, 2)
                result = store.advance_effect(ref, outcome, expected_revision=2,
                                              outcome=outcome, evidence_refs=("observed",))
                self.assertEqual(result.outcome, outcome)
            result = store.advance_effect(ref, "unknown", expected_revision=3,
                                          outcome="succeeded", evidence_refs=("reconciled",))
            self.assertEqual(result.revision, 4)
            store.advance_effect(ref, "blocked", expected_revision=1,
                                 outcome="failed_no_effect", evidence_refs=("cancelled before dispatch",))

    @unittest.skipUnless(shutil.which("tsc"), "TypeScript compiler required")
    def test_typescript_process_outcome_schema_matches_python(self):
        from puppetmaster.consumption import build_attempt_consumption_report
        client = Path(__file__).resolve().parents[1] / "clients/typescript/puppetmaster.ts"
        # Compile the exported wire declarations, independent of Node runtime types.
        declarations = client.read_text().split("export interface LegacyJobRef", 1)[1]
        declarations = "export interface LegacyJobRef" + declarations
        for store, job, task, run, ref in self.stores():
            attempt = ExecutionAttempt.from_run(run, adapter="local")
            store.record_attempt(attempt)
            observations = [UsageObservation(job.id, attempt.attempt_id, str(i), "process", run.started_at,
                                            returncode=code, timed_out=timeout)
                            for i, (code, timeout) in enumerate(((0, False), (None, True), (1, None), (None, None)))]
            for obs in observations:
                store.record_usage_observation(obs)
            report = build_attempt_consumption_report(store, job.id)
            fixture = declarations + "\nconst report: AttemptConsumptionReport = " + json.dumps(to_jsonable(report)) + ";\n"
            for obs in observations:
                value = to_jsonable(obs)
                fixture += "const o" + obs.observation_id + ": ProcessOutcomeObservation = " + json.dumps(value) + ";\n"
            fixture += "const legacy: ProcessOutcomeObservation = { ...o0, returncode: undefined, timed_out: undefined };\n"
            fixture += "// @ts-expect-error exit facts must not accept strings\nconst bad: ProcessOutcomeObservation = { ...o0, returncode: 'zero' };\n"
            fixture += "// @ts-expect-error timeout must be boolean\nconst badTimeout: ProcessOutcomeObservation = { ...o0, timed_out: 1 };\n"
            fixture += "const outcomes: readonly ProcessOutcomeObservation[] = report.attempts[0].process_outcomes;\n"
            with TemporaryDirectory() as tmp:
                source = Path(tmp) / "wire.ts"
                source.write_text(fixture)
                result = subprocess.run(["tsc", "--noEmit", "--strict", "--skipLibCheck", str(source)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
