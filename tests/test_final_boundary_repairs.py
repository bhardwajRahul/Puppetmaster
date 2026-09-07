"""Exact adversarial inputs from the final v1.25 review."""
import base64
import hmac
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from puppetmaster import readonly
from puppetmaster.attempts import ExecutionAttempt, UsageObservation
from puppetmaster.contracts import CursorCodec
from puppetmaster.models import AgentRun, JobRef
from puppetmaster.projections import connection
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore

DEEP = '[' * 1100 + '0' + ']' * 1100
BAD_NUMBERS = ('1' + '0' * 400, '1e308', '1e999', 'NaN', 'Infinity', '-Infinity', 'true')


class Sentinel(str):
    def forbidden(self, *args, **kwargs):
        raise AssertionError('subclass callback before rejection')
    replace = encode = __len__ = __str__ = __hash__ = __iter__ = isascii = forbidden
    __reduce_ex__ = forbidden


class FinalBoundaryRepairs(unittest.TestCase):
    def stores(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with TemporaryDirectory() as tmp:
                store = cls(Path(tmp) / 'state')
                job = store.create_job('boundary')
                ref = store.job_ref(job.id)
                for i in range(2):
                    store.record_attempt(ExecutionAttempt(job.id, 'task', 'run'+str(i), 'a'+str(i), 'now', 'codex'))
                    store.record_usage_observation(UsageObservation(job.id, 'a'+str(i), 'o', 'process', 'now', returncode=0))
                    store.save_run(AgentRun(job.id, 'task', 'explore', 'worker', id='run'+str(i)))
                yield store, job, ref

    def test_job_ref_rejects_before_callbacks_or_serialization(self):
        for field in ('job_id', 'state_id', 'incarnation'):
            for value in (Sentinel('x' * 1000000), 'x' * 1000000, Sentinel('a'), 1, True, [], {}, b'a', '\u00e9'):
                args = dict(job_id='job_a', state_id='state_a', version=2,
                            incarnation='00000000-0000-0000-0000-000000000001')
                args[field] = value
                with self.subTest(field=field, kind=type(value).__name__), \
                        patch('uuid.UUID', side_effect=AssertionError('UUID before scalar guards')), \
                        patch('json.dumps', side_effect=AssertionError('serialization before rejection')):
                    with self.assertRaises(ValueError):
                        JobRef(**args)
        self.assertEqual(JobRef('legacy', 'state').as_dict(), dict(job_id='legacy', state_id='state'))

    def test_numeric_ledger_rejects_before_float_conversion(self):
        for field in ('cost_usd', 'tokens_in', 'returncode'):
            for value in (10**400, 1e308, float('inf'), float('nan'), True):
                args = dict(job_id='j', attempt_id='a', observation_id='o', source='s', observed_at='now')
                args[field] = value
                with self.subTest(field=field, value=type(value).__name__), self.assertRaises(ValueError):
                    UsageObservation(**args)

    def test_maximum_valid_economics_roundtrip(self):
        for store, job, ref in self.stores():
            value = UsageObservation(job.id, 'a0', 'max', 'provider', 'now',
                                     usage_state='measured', tokens_in=2**63-1,
                                     cost_state='measured', cost_usd=1_000_000_000_000, cost_basis='api')
            store.record_usage_observation(value)
            page = store.list_usage_observation_refs(ref)
            self.assertEqual(page.outcome, 'complete')
            facts = next(item.facts for item in page.items if item.facts['observation_id'] == 'max')
            self.assertEqual(facts['tokens_in'], value.tokens_in)
            self.assertEqual(facts['cost_usd'], value.cost_usd)

    def test_history_malformed_initial_and_continuation(self):
        for store, job, ref in self.stores():
            for kind, read in (('attempt', store.list_attempt_refs), ('run', store.list_run_refs),
                               ('observation', store.list_usage_observation_refs), ('outcome', store.list_process_outcome_refs)):
                first = read(ref, limit=1)
                self.assertIsNotNone(first.next_cursor)
                with connection(store) as c:
                    row = c.execute('SELECT sequence,facts FROM historical_refs WHERE kind=? AND job_id=? ORDER BY sequence DESC LIMIT 1', (kind, job.id)).fetchone()
                valid = json.loads(row['facts'])
                bad = [DEEP, '{"unused":' + DEEP + '}']
                fields = ('cost_usd', 'tokens_in', 'returncode') if kind in ('observation', 'outcome') else ('task_id' if kind == 'attempt' else 'status',)
                for field in fields:
                    for number in BAD_NUMBERS:
                        facts = dict(valid)
                        facts[field] = '__bad__'
                        bad.append(json.dumps(facts).replace('"__bad__"', number))
                for facts in bad:
                    with connection(store) as c:
                        c.execute('UPDATE historical_refs SET facts=? WHERE sequence=?', (facts, row['sequence']))
                    for cursor in (None, first.next_cursor):
                        with self.subTest(backend=store.backend_name, kind=kind, cursor=bool(cursor)):
                            page = read(ref, cursor=cursor)
                            self.assertEqual(page.outcome, 'unavailable')
                            self.assertEqual(page.next_cursor, cursor)
                            self.assertFalse(page.items)
                with connection(store) as c:
                    c.execute('UPDATE historical_refs SET facts=? WHERE sequence=?', (row['facts'], row['sequence']))

    def test_scope_and_counts_preserve_continuation_checkpoint(self):
        for store, job, ref in self.stores():
            store.create_job('second')
            store.create_job('third')
            for read in (store.list_job_summaries, store.read_job_summary_changes):
                checkpoint = 1 if read == store.read_job_summary_changes else 0
                options = {'after_revision': checkpoint} if checkpoint else {}
                first = read(limit=1, **options)
                self.assertIsNotNone(first.next_cursor)
                for bad in (DEEP, '{"origin":' + DEEP + '}') + tuple('{"origin":'+n+'}' for n in BAD_NUMBERS):
                    with connection(store) as c:
                        c.execute('UPDATE projection_versions SET scope=?', (bad,))
                        c.execute('UPDATE projection_changes SET scope=?', (bad,))
                    for cursor in (None, first.next_cursor):
                        page = read(cursor=cursor, **options)
                        self.assertEqual(page.outcome, 'unavailable')
                        self.assertEqual(page.next_cursor, cursor)
                        self.assertEqual(page.revision, checkpoint)
                    with connection(store) as c:
                        c.execute("UPDATE projection_versions SET scope='{}'")
                        c.execute("UPDATE projection_changes SET scope='{}'")
            for number in BAD_NUMBERS[:-1]:
                with connection(store) as c:
                    c.execute('UPDATE historical_counts SET count=?', (number,))
                self.assertEqual(store.historical_evidence_counts(ref).outcome, 'unavailable')
                self.assertEqual(store.list_attempt_refs(ref).outcome, 'unavailable')

    def test_resolved_attempt_depth_is_unavailable(self):
        for store, job, ref in self.stores():
            for read in (store.list_usage_observation_refs, store.list_process_outcome_refs):
                first = read(ref, limit=1)
                with connection(store) as c:
                    original = c.execute("SELECT facts FROM historical_refs WHERE kind='attempt' AND id='a1'").fetchone()[0]
                    c.execute("UPDATE historical_refs SET facts=? WHERE kind='attempt' AND id='a1'", (DEEP,))
                for cursor in (None, first.next_cursor):
                    page = read(ref, cursor=cursor)
                    self.assertEqual(page.outcome, 'unavailable')
                    self.assertEqual(page.next_cursor, cursor)
                with connection(store) as c:
                    c.execute("UPDATE historical_refs SET facts=? WHERE kind='attempt' AND id='a1'", (original,))

    def test_completion_receipts_reject_malformed_scalars(self):
        for store, job, ref in self.stores():
            for bad in (DEEP,) + BAD_NUMBERS:
                with connection(store) as c:
                    c.execute("INSERT OR REPLACE INTO completion_receipts VALUES(?,?,?,?)", (job.id, 'r', bad, bad))
                receipt = store.get_completion_receipt(ref, 'r')
                self.assertEqual(receipt.outcome, 'unavailable')
                self.assertIsNone(receipt.intent_digest)
            with self.assertRaises(ValueError):
                store.get_completion_receipt(ref, Sentinel('x' * 1000000))

    def test_cursor_inspection_and_authentication(self):
        codec = CursorCodec(b'secret')
        for body in (DEEP, '{"v":1,"scope":"s","nested":'+DEEP+'}') + tuple('{"v":1,"scope":"s","count":'+n+'}' for n in BAD_NUMBERS[:-1]):
            raw = body.encode()
            for signature in (b'x'*32, hmac.digest(codec.secret, raw, 'sha256')):
                token = base64.urlsafe_b64encode(signature+raw).decode()
                for operation in (CursorCodec.inspect, lambda t: codec.decode(t, 's')):
                    with self.assertRaises(ValueError):
                        operation(token)

    def test_exact_worker_lock_codes_have_short_ordinary_budget(self):
        for reuse, budget, attempts in ((True, .1, 2), (False, 1.0, 20)):
            for code in (5, 6, 261, 262, 517):
                with TemporaryDirectory() as root:
                    store = SwarmStore(root)
                    clock = [0.0]
                    error = readonly.ReadUnavailable('unable to open database: active reader; sidecars may be missing') if code == 5 else sqlite3.OperationalError('contended')
                    error.sqlite_errorcode = code
                    def sleep(seconds):
                        clock[0] += seconds
                    with patch.object(readonly, 'ReadConnection', side_effect=error) as opens, \
                            patch.object(readonly.time, 'monotonic', side_effect=lambda: clock[0]), \
                            patch.object(readonly.time, 'sleep', side_effect=sleep):
                        with self.assertRaises(sqlite3.OperationalError):
                            readonly.connect(store, reuse=reuse)
                    self.assertAlmostEqual(clock[0], budget)
                    self.assertEqual(opens.call_count, attempts)


if __name__ == '__main__':
    unittest.main()
