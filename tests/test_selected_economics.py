from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sys

from readonly_fixtures import replacement_blocked

from puppetmaster.models import JobStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class SelectedEconomicsTests(unittest.TestCase):
    def stores(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with TemporaryDirectory() as tmp:
                yield cls(Path(tmp) / 'state')

    def test_display_and_revision(self):
        for store in self.stores():
            job = store.create_job('a' * 511 + '😀tail')
            row = store.list_job_summaries().items[0]
            self.assertEqual(row.goal_preview, 'a' * 511)
            self.assertTrue(row.goal_preview_truncated)
            self.assertEqual((row.delivery, row.quality), ('pending', 'unverified'))
            result = store.get_selected_economics(store.job_ref(job.id))
            self.assertEqual(result.reason, 'no_terminal_receipt')
            store.update_job_status(job.id, JobStatus.COMPLETE)
            row2 = store.list_job_summaries().items[0]
            self.assertEqual(row2.delivery, 'unverified')
            self.assertEqual(store.get_selected_economics(store.job_ref(job.id),
                expected_summary_revision=row.revision).reason, 'selection_changed')

    def usage(self, store, job, *, task_id='t', cost=3, result='passed', sdk=None, legacy=False):
        from puppetmaster.models import Artifact, ArtifactType
        from puppetmaster.usage import token_usage
        payload = dict(check='test', result=result, model='mid-v1', real_cost_usd=cost)
        payload.update(token_usage(sdk_usage={'inputTokens': 10, 'outputTokens': 2} if sdk is None else sdk))
        if legacy:
            payload.pop('selected_facts', None)
        artifact = Artifact(job_id=job.id, task_id=task_id, type=ArtifactType.VERIFICATION,
                            created_by='worker', confidence=1, evidence=['test'], payload=payload)
        store.save_artifact(artifact)
        return artifact

    def freeze_job(self, store, job):
        from unittest.mock import patch
        from tests.test_cost_report import _registry
        with patch('puppetmaster.cost.load_registry', return_value=_registry()):
            store.update_job_status(job.id, JobStatus.COMPLETE)
        return store.get_selected_economics(store.job_ref(job.id))

    def test_unicode_boundaries_and_huge_goals(self):
        from puppetmaster.job_display import preview
        values = ['', 'a'*511, 'a'*512, 'a'*513, '界'*171, '😀'*129,
                  'a'*509 + '界x', 'e\u0301'*200, '👩\u200d💻'*60,
                  '\0' + 'a'*510 + '😀', 'a'*511 + '\0x', 'x'*1_000_000]
        for store in self.stores():
            job = store.create_job('seed')
            for goal in values:
                with self.subTest(backend=store.backend_name, length=len(goal), prefix=goal[:8]):
                    store.save_job(replace(job, goal=goal))
                    row = store.list_job_summaries().items[0]
                    self.assertEqual((row.goal_preview, row.goal_preview_truncated), preview(goal))
                    self.assertLessEqual(len(row.goal_preview.encode()), 512)
                    self.assertTrue(goal.startswith(row.goal_preview))
            # JSON permits escaped lone surrogates; they are not Unicode scalars.
            for goal in ('\ud800', 'a'*512 + '\udfff', '\0\ud800', None, 123):
                store.save_job(replace(job, goal=goal))
                row = store.list_job_summaries().items[0]
                self.assertIsNone(row.goal_preview)
                self.assertIsNone(row.goal_preview_truncated)
        class BoundedString(str):
            def __getitem__(self, key):
                self.assertion(key)
                return super().__getitem__(key)
            def assertion(self, key):
                if not isinstance(key, slice) or key.stop > 513:
                    raise AssertionError('unbounded goal inspection')
            def encode(self, *args, **kwargs):
                raise AssertionError('encoded full goal')
        self.assertEqual(preview(BoundedString('x'*1_000_000)), ('x'*512, True))

    def test_lifecycle_and_child_history_journal_copy(self):
        from puppetmaster.attempts import ExecutionAttempt, UsageObservation
        from puppetmaster.models import Task
        mapping = dict(queued='pending', running='pending', stitching='pending',
                       complete='unverified', failed='blocked', cancelled='blocked', stalled='blocked')
        for store in self.stores():
            job = store.create_job('goal')
            ref = store.job_ref(job.id)
            for status, delivery in mapping.items():
                store.save_job(replace(job, status=JobStatus(status)))
                row = store.list_job_summaries().items[0]
                self.assertEqual((row.delivery, row.quality), (delivery, 'unverified'))
            store.save_task(Task(job_id=job.id, role='test', instruction='private'))
            before = store.list_job_summaries().items[0]
            attempt = ExecutionAttempt(job.id, 't', 'r', 'a', 'now', 'codex')
            store.record_attempt(attempt)
            after = store.list_job_summaries().items[0]
            self.assertGreater(after.revision, before.revision)
            self.assertEqual((after.goal_preview, after.delivery), ('goal', 'blocked'))
            store.record_attempt(attempt)
            self.assertEqual(store.list_job_summaries().items[0].revision, after.revision)
            store.record_usage_observation(UsageObservation(job.id,'a','o','sdk','now', cost_usd=7, cost_state='measured',cost_basis='api'))
            changes = store.read_job_summary_changes(after_revision=before.revision)
            self.assertTrue(changes.items)
            for row in changes.items:
                self.assertEqual((row.goal_preview, row.delivery, row.quality), ('goal', 'blocked', 'unverified'))

    def test_selected_not_attempted_freeze_late_replay_and_conflict(self):
        from puppetmaster.attempts import ExecutionAttempt, UsageObservation
        from puppetmaster.consumption import build_attempt_consumption_report
        from puppetmaster.models import to_jsonable
        import copy
        import sqlite3
        from puppetmaster.contracts import ContractConflict
        for store in self.stores():
            job = store.create_job('selected economics')
            self.usage(store, job, cost=7, result='failed')
            self.usage(store, job, cost=3)
            for ident, amount in (('failed',7), ('success',3)):
                store.record_attempt(ExecutionAttempt(job.id,'t',ident,ident,'now','codex'))
                store.record_usage_observation(UsageObservation(job.id,ident,ident,'sdk','now',cost_state='measured',cost_basis='api',cost_usd=amount))
            frozen = self.freeze_job(store, job)
            self.assertEqual(frozen.outcome, 'available')
            self.assertEqual(frozen.selected_count, 1)
            self.assertEqual(frozen.totals.api_cost_usd.total, 3)
            self.assertEqual(build_attempt_consumption_report(store, job.id).totals.api_cost_usd.total, 10)
            self.assertEqual(frozen.totals.tokens_in.total, 10)
            self.assertIsNone(frozen.totals.cache_read_tokens.total)
            self.usage(store, job, cost=900, sdk={'inputTokens':20, 'outputTokens':2})
            store.record_usage_observation(UsageObservation(job.id,'success','late','sdk','later',cost_state='measured',cost_basis='api',cost_usd=4))
            late = store.get_selected_economics(store.job_ref(job.id))
            self.assertEqual(late.totals, frozen.totals)
            self.assertEqual(late.receipt_digest, frozen.receipt_digest)
            self.assertGreater(late.summary_revision, frozen.summary_revision)
            source = store.get_job(job.id)
            store.save_job(source)
            conflicting = copy.deepcopy(source.cost_receipt)
            conflicting['actual_cost']['total_marginal_cost_usd'] = 999
            with self.assertRaises((ContractConflict, sqlite3.IntegrityError)):
                store.save_job(replace(source, cost_receipt=conflicting))
            self.assertEqual(store.get_selected_economics(store.job_ref(job.id)).totals, frozen.totals)
            if store.backend_name == 'file':
                store.repair_metadata_index()
                self.assertEqual(store.get_selected_economics(store.job_ref(job.id)).totals, frozen.totals)
            store.update_job_status(job.id, JobStatus.RUNNING)
            self.assertEqual(store.get_selected_economics(store.job_ref(job.id)).reason,'no_terminal_receipt')
            renewed = self.freeze_job(store, job)
            self.assertEqual(renewed.totals.api_cost_usd.total, 900)
            store.delete_job(job.id)
            with self.assertRaises(KeyError):
                store.get_selected_economics(frozen.job_ref)

    def test_zero_estimates_unknown_and_mixed_bases(self):
        from tests.test_cost_report import _routing
        for store in self.stores():
            job = store.create_job('zero')
            self.usage(store, job, cost=0, sdk={'inputTokens': 0})
            zero = self.freeze_job(store, job)
            self.assertEqual((zero.totals.api_cost_usd.total, zero.totals.api_cost_usd.state), (0,'measured'))
            self.assertEqual((zero.totals.tokens_in.total,zero.totals.tokens_in.state),(0,'measured'))
            self.assertIsNone(zero.totals.tokens_out.total)
            job2 = store.create_job('legacy token ambiguity')
            self.usage(store, job2, legacy=True)
            legacy = self.freeze_job(store, job2)
            self.assertIsNone(legacy.totals.tokens_in.total)
            self.assertEqual(legacy.totals.api_cost_usd.total,3)
            job3 = store.create_job('mixed')
            self.usage(store, job3, task_id='api', cost=3)
            self.usage(store, job3, task_id='plan', cost=None)
            store.save_artifact(_routing(job3.id,'plan','mid-model',billing='plan'))
            mixed = self.freeze_job(store, job3)
            for field in ('api_cost_usd','plan_marginal_cost_usd'):
                metric = getattr(mixed.totals,field)
                self.assertIsNone(metric.total)
                self.assertEqual((metric.state,metric.known_selected,metric.unknown_selected),('partial',1,1))
            job4 = store.create_job('estimated')
            self.usage(store, job4, cost=None, sdk={})
            estimated = self.freeze_job(store,job4)
            self.assertEqual((estimated.totals.tokens_in.total,estimated.totals.tokens_in.state),(0,'estimated'))
            self.assertEqual(estimated.totals.api_equivalent_cost_usd.state,'estimated')
            self.assertIsNone(estimated.totals.api_cost_usd.total)

    def test_legacy_billing_reopen_matches_terminal_receipt(self):
        from unittest.mock import patch
        from puppetmaster.cost import build_cost_report, execution_billing_artifacts
        from puppetmaster.models import Artifact, ArtifactType, Task, to_jsonable
        from tests.test_cost_report import _registry

        for store_type in (SwarmStore, SQLiteSwarmStore):
            for billing in ('unknown', 'api', 'plan'):
                for provenance in (None, 'unknown', 'api', 'plan', 'inconclusive'):
                    with self.subTest(store=store_type.__name__, billing=billing,
                                      provenance=provenance), TemporaryDirectory() as tmp:
                        store = store_type(Path(tmp) / 'state')
                        job = store.create_job('legacy reported cost')
                        task = Task(job_id=job.id, role='explore', instruction='legacy',
                                    payload={'model': 'mid-v1'})
                        store.save_task(task)
                        self.usage(store, job, task_id=task.id, cost=5)
                        if provenance is not None:
                            store.save_artifact(Artifact(job_id=job.id, task_id=task.id,
                                type=ArtifactType.VERIFICATION, created_by='orchestrator',
                                confidence=1, evidence=['execution billing'], payload={
                                    'check': 'execution_billing', 'result': 'passed', 'model_id': 'mid-model',
                                    'billing': provenance}))
                        registry = [replace(_registry()[0], billing=billing)]
                        with patch('puppetmaster.cost.load_registry', return_value=registry):
                            store.update_job_status(job.id, JobStatus.COMPLETE)
                        frozen = store.get_selected_economics(store.job_ref(job.id))
                        reopened = store_type(Path(tmp) / 'state')
                        selected = reopened.get_selected_economics(reopened.job_ref(job.id))
                        self.assertEqual(selected, frozen)
                        receipt = reopened.get_job(job.id).cost_receipt
                        self.assertEqual(build_cost_report(reopened, job.id, []), receipt)
                        self.assertEqual(to_jsonable(selected.totals),
                                         receipt['bounded_economics']['totals'])
                        effective = provenance if provenance in ('unknown', 'api', 'plan') else billing
                        row = receipt['actual_cost']['tasks'][0]
                        self.assertEqual(row['billing'], effective)
                        expected = 5 if effective == 'api' else 0 if effective == 'plan' else None
                        self.assertEqual(receipt['actual_cost']['total_marginal_cost_usd'], expected)
                        api = selected.totals.api_cost_usd
                        self.assertEqual((api.total, api.state),
                                         (5, 'measured') if effective == 'api' else (None, 'unknown'))
                        plan = selected.totals.plan_marginal_cost_usd
                        self.assertEqual((plan.total, plan.state),
                                         (0, 'estimated') if effective == 'plan' else (None, 'unknown'))
                        equivalent = selected.totals.api_equivalent_cost_usd
                        self.assertEqual((equivalent.total, equivalent.state),
                                         (0.00006, 'estimated') if effective == 'unknown' else (None, 'unknown'))
                        sources = execution_billing_artifacts(reopened.list_artifacts(job.id))
                        if provenance is None:
                            self.assertEqual(sources, {})
                        else:
                            self.assertEqual(sources[task.id].payload['billing'], provenance)

    def test_no_source_reads_and_constant_lookup_work(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        import json
        from puppetmaster.models import to_jsonable
        from puppetmaster.projections import connection
        from tests.test_v1250_release_blockers import measured_reads
        for store in self.stores():
            job = store.create_job('body'*10000)
            self.usage(store,job)
            frozen = self.freeze_job(store,job)
            for size in (10,100000):
                # Writer bulk fixture; public lookup must never visit history.
                with connection(store) as c:
                    c.executemany("INSERT OR IGNORE INTO historical_refs(kind,job_id,id,facts) VALUES('attempt',?,?, '{}')",
                                  ((job.id,str(n)) for n in range(size)))
                with ExitStack() as stack:
                    for name in ('get_job','list_tasks','list_artifacts','read_json','list_attempts','list_usage_observations'):
                        stack.enter_context(patch.object(store,name,side_effect=AssertionError('source read')))
                    stack.enter_context(patch('puppetmaster.cost.build_cost_report',side_effect=AssertionError('cost scan')))
                    with measured_reads(deny_bodies=True) as measured:
                        result = store.get_selected_economics(frozen.job_ref)
                    self.assertEqual(result.totals, frozen.totals)
                    self.assertLess(measured['rows'],20)
                    self.assertLess(measured['bytes'],8192)
                    self.assertFalse(measured['writes'])
                    self.assertFalse(any(t in ('historical_refs','historical_counts') for t,_ in measured['reads']))
                    self.assertLessEqual(len(json.dumps(to_jsonable(result)).encode()),8192)

    def test_snapshot_fields_revision_tombstone(self):
        for store in self.stores():
            jobs = [store.create_job(str(n)) for n in range(3)]
            first = store.list_job_summaries(limit=1)
            seen = first.items[0].id
            target = next(j for j in jobs if j.id != seen)
            original = target.goal
            store.save_job(replace(target,goal='changed',status=JobStatus.COMPLETE))
            page = store.list_job_summaries(cursor=first.next_cursor,limit=2)
            row = next(r for r in page.items if r.id == target.id)
            self.assertEqual((row.goal_preview,row.delivery),(original,'pending'))
            self.assertEqual(store.get_selected_economics(row.job_ref,expected_summary_revision=row.revision).reason,'selection_changed')
            revision = store.list_job_summaries().revision
            store.delete_job(target.id)
            tombstone = next(r for r in store.read_job_summary_changes(after_revision=revision).items if r.id==target.id)
            self.assertTrue(tombstone.deleted)
            self.assertEqual((tombstone.goal_preview,tombstone.delivery),('changed','unverified'))

    def test_numeric_limits_and_malformed_projection(self):
        import json
        from puppetmaster.projections import connection
        from puppetmaster.selected_economics import MAX_INTEGER, project, validate_payload
        from puppetmaster.identity import StoreIdentityError
        from puppetmaster.models import JobRef
        for store in self.stores():
            job = store.create_job('bounds')
            self.usage(store,job,cost=1e12,sdk={'inputTokens':MAX_INTEGER,'outputTokens':0})
            frozen = self.freeze_job(store,job)
            self.assertEqual(frozen.totals.tokens_in.total,MAX_INTEGER)
            self.assertEqual(frozen.totals.api_cost_usd.total,1e12)
            for bad in (-1,True,1.0,MAX_INTEGER+1,'1'):
                with self.assertRaises(ValueError):
                    store.get_selected_economics(frozen.job_ref,expected_summary_revision=bad)
            with self.assertRaises(StoreIdentityError):
                store.get_selected_economics(JobRef(job.id,frozen.job_ref.state_id))
            for value in ('[]','{}','{"version":1}',json.dumps({'version':1,'receipt_digest':None,'selected_count':None,'totals':None,'reason':'bad'})):
                with connection(store) as c:
                    c.execute('UPDATE selected_economics_current SET payload=? WHERE job_id=?',(value,job.id))
                self.assertEqual(store.get_selected_economics(frozen.job_ref).reason,'metadata_invalid')
            job2=store.create_job('overflow')
            self.usage(store,job2,sdk={'inputTokens':MAX_INTEGER+1,'outputTokens':0})
            self.assertEqual(self.freeze_job(store,job2).reason,'numeric_limit')

    def test_legacy_attach_and_migration_preserve_identity(self):
        import sqlite3
        from puppetmaster.projections import connection
        from unittest.mock import patch
        for store in self.stores():
            job = store.create_job('legacy goal')
            identity=store.incarnation
            secret=None
            with connection(store) as c:
                secret=c.execute("SELECT value FROM projection_meta WHERE key='secret'").fetchone()[0]
                c.execute("DELETE FROM projection_meta WHERE key='display_economics_version'")
                if store.backend_name=='sqlite':
                    c.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
            reader=type(store)(store.root)
            with patch.object(reader,'init',side_effect=AssertionError('read-time migration')):
                row=reader.list_job_summaries().items[0]
                self.assertEqual((row.goal_preview,row.delivery),(None,'unavailable'))
            reader.init()
            self.assertEqual(reader.incarnation,identity)
            self.assertEqual(reader.list_job_summaries().items[0].goal_preview,'legacy goal')
            with connection(reader) as c:
                self.assertEqual(c.execute("SELECT value FROM projection_meta WHERE key='secret'").fetchone()[0],secret)
            second=type(store)(store.root)
            second.init()
            self.assertEqual(second.incarnation,identity)

    def test_cli_mcp_and_python39_wire(self):
        import ast
        import contextlib
        import io
        import json
        from puppetmaster.cli import main
        from puppetmaster.mcp_server import run_job_summaries,run_job_summary_changes,run_selected_economics
        for store in self.stores():
            job=store.create_job('wire')
            ref=store.job_ref(job.id)
            args={'state_dir':str(store.root),'backend':store.backend_name,'job_ref':ref.as_dict()}
            for command,handler in (('job-summaries',run_job_summaries),('job-summary-changes',run_job_summary_changes),('selected-economics',run_selected_economics)):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc=main(['--state-dir',str(store.root),'--backend',store.backend_name,'--job-ref',json.dumps(ref.as_dict()),command,'--json'])
                self.assertEqual(rc,0)
                cli=json.loads(out.getvalue())
                mcp=json.loads(handler(args)['content'][0]['text'])
                self.assertEqual(cli,mcp)
                self.assertLessEqual(len(out.getvalue().encode()),262145 if command!='selected-economics' else 8193)
        for path in ('puppetmaster/job_display.py','puppetmaster/selected_economics.py','puppetmaster/projections.py'):
            ast.parse(Path(path).read_text(),feature_version=(3,9))

    def test_pending_and_same_path_replacement(self):
        import sqlite3
        from puppetmaster.projections import connection
        from puppetmaster.identity import StoreIdentityError
        for store in self.stores():
            job=store.create_job('identity')
            self.usage(store,job)
            frozen=self.freeze_job(store,job)
            with connection(store) as c:
                c.execute("INSERT INTO projection_pending VALUES('interrupted')")
            self.assertEqual(store.get_selected_economics(frozen.job_ref).reason,'projection_pending')
            with connection(store) as c:
                c.execute('DELETE FROM projection_pending')
            root=store.root
            root.rename(root.parent/'old')
            replacement=type(store)(root)
            replacement.create_job('new store')
            with self.assertRaises(StoreIdentityError):
                replacement.get_selected_economics(frozen.job_ref)
            with self.assertRaises(StoreIdentityError):
                store.get_selected_economics(frozen.job_ref)

    def test_validation_and_revision_use_same_snapshot(self):
        from unittest.mock import patch
        from puppetmaster.identity import validate
        import threading
        for store in self.stores():
            job=store.create_job('snapshot')
            self.usage(store,job)
            frozen=self.freeze_job(store,job)
            started=threading.Event()
            errors=[]
            def reset():
                started.set()
                try:
                    store.update_job_status(job.id,JobStatus.RUNNING)
                except Exception as exc:
                    errors.append(exc)
            worker=threading.Thread(target=reset)
            def concurrent_reset(current, ref, c, **kwargs):
                validate(current,ref,c,**kwargs)
                worker.start()
                self.assertTrue(started.wait(1))
            with patch('puppetmaster.identity.validate',side_effect=concurrent_reset):
                selected=store.get_selected_economics(frozen.job_ref,expected_summary_revision=frozen.summary_revision)
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[])
            if selected.outcome == 'available':
                self.assertEqual(selected, frozen)
            else:
                # The immutable reader may refuse a concurrently changed file;
                # it must never combine the old revision with new economics.
                self.assertEqual(selected.reason, 'read_snapshot_unavailable')
                self.assertIsNone(selected.totals)
            self.assertEqual(store.get_selected_economics(frozen.job_ref).reason,'no_terminal_receipt')

    def test_first_terminal_writer_wins_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        from puppetmaster.cost import build_current_registry_cost_report
        from puppetmaster.selected_economics import freeze
        from tests.test_cost_report import _registry, _usage
        from puppetmaster.contracts import ContractConflict
        import sqlite3
        for store in self.stores():
            job=store.create_job('first writer')
            candidates=[]
            for amount in (3,7):
                artifacts=[_usage(job.id,real_cost_usd=amount)]
                receipt=build_current_registry_cost_report(job.id,artifacts,_registry())
                receipt['pricing_source']='terminal_receipt'
                receipt['bounded_economics']=freeze(receipt,artifacts)
                candidates.append(replace(job,status=JobStatus.COMPLETE,cost_receipt=receipt))
            def write(candidate):
                try:
                    store.save_job(candidate)
                    return True
                except (ContractConflict,sqlite3.IntegrityError):
                    return False
            with ThreadPoolExecutor(2) as pool:
                accepted=list(pool.map(write,candidates))
            self.assertEqual(sum(accepted),1)
            expected=candidates[accepted.index(True)].cost_receipt['bounded_economics']
            self.assertEqual(store.get_selected_economics(store.job_ref(job.id)).receipt_digest,expected['receipt_digest'])
            self.assertEqual(store.get_job(job.id).cost_receipt['bounded_economics'],expected)

    def test_typescript_runtime_wire_and_one_page_wrappers(self):
        import json
        import shutil
        import subprocess
        import sys
        from puppetmaster.models import to_jsonable
        compiler=shutil.which('tsc')
        node=shutil.which('node')
        type_roots=Path('/Users/carypalmer/.local/node/lib/node_modules/vercel/node_modules/@types')
        if not compiler or not node or not type_roots.is_dir():
            self.skipTest('TypeScript runtime toolchain unavailable')
        with TemporaryDirectory() as tmp:
            result=subprocess.run([compiler,'--strict','--skipLibCheck','--target','es2022','--module','node16',
                '--moduleResolution','node16','--typeRoots',str(type_roots),'--types','node','--outDir',tmp,
                'clients/typescript/puppetmaster.ts'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            (Path(tmp)/'package.json').write_text('{"type":"module"}')
            script=Path(tmp)/'verify.mjs'
            script.write_text('''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as client from './puppetmaster.js';
const f = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
 assert.deepEqual(client.decodeSelectedEconomics(f.economics, f.ref), f.economics);
 const unsupported = client.decodeSelectedEconomics({}, f.ref);
 assert.equal(unsupported.outcome, 'unavailable');
 assert.equal(unsupported.totals, null);
 const old = structuredClone(f.page);
 for (const row of old.items) for (const key of ['goal_preview','goal_preview_truncated','delivery','quality','previous_status','previous_origin','previous_project_id','previous_session_id','previous_membership']) delete row[key];
 delete old.reason;
 delete old.retry_after_ms;
 const decoded = client.decodeMetadataPage(old);
 assert.equal(decoded.items[0].delivery, 'unavailable');
 assert.equal(decoded.items[0].previous_membership, 'unavailable');
 assert.equal(decoded.items[0].goal_preview, null);
 assert.equal(decoded.items[0].goal_preview_truncated, null);
 for (const pair of [{goal_preview:'x'}, {goal_preview:null}, {goal_preview_truncated:true},
     {goal_preview_truncated:null}, {goal_preview:undefined,goal_preview_truncated:undefined},
     {goal_preview:'x',goal_preview_truncated:'false'}, {goal_preview:3,goal_preview_truncated:false},
     {goal_preview:[],goal_preview_truncated:false}, {goal_preview:'x',goal_preview_truncated:0},
     {goal_preview:null,goal_preview_truncated:false}, {goal_preview:'x',goal_preview_truncated:null}]) {
   const bad = structuredClone(old);
   Object.assign(bad.items[0], pair);
   assert.throws(() => client.decodeMetadataPage(bad));
 }
 for (const pair of [{goal_preview:null,goal_preview_truncated:null},
     {goal_preview:'',goal_preview_truncated:false}, {goal_preview:'x',goal_preview_truncated:true}]) {
   const valid = structuredClone(old);
   Object.assign(valid.items[0], pair);
   const row = client.decodeMetadataPage(valid).items[0];
   assert.equal(row.goal_preview, pair.goal_preview);
   assert.equal(row.goal_preview_truncated, pair.goal_preview_truncated);
 }
 assert.equal(decoded.reason, null);
 assert.equal(decoded.retry_after_ms, null);
 const task = structuredClone(old);
 task.items[0].kind = 'task';
 task.items[0].binding = {task_id:'task', generation:null, lease_id:null, owner:null};
 assert.equal(client.decodeMetadataPage(task).items[0].binding.generation, null);
 for (const generation of [-1, '0', true, 1.5, undefined]) {
   task.items[0].binding.generation = generation;
   assert.throws(() => client.decodeMetadataPage(task));
 }
 for (const [key, value] of [['reason',3],['retry_after_ms',-1]]) {
   assert.throws(() => client.decodeMetadataPage({...old,[key]:value}));
 }
 for (const [key, value] of [['previous_status',3],['previous_membership',null],['previous_membership',['present']],['kind',['job']],['delivery',['pending']]]) {
   const bad = structuredClone(old);
   bad.items[0][key] = value;
   assert.throws(() => client.decodeMetadataPage(bad));
 }
 const page = await client.listJobSummaries(f.client, {limit:1});
 assert.equal(page.items.length,1);
 assert.equal(page.outcome,'partial');
 assert.ok(page.next_cursor);
 const selected = await client.getSelectedEconomics(f.ref, f.client, f.economics.summary_revision);
 assert.deepEqual(selected, f.economics);
 const changes = await client.readJobSummaryChanges(f.client, {limit:1});
 assert.equal(changes.items.length,1);
 assert.equal(changes.outcome,'partial');
 const tombstone = await client.readJobSummaryChanges(f.client, {job_ref:f.deletedRef, after_revision:f.checkpoint});
 assert.deepEqual(tombstone.items.map(row => [row.id,row.deleted]), [[f.deletedRef.job_id,true]]);
 assert.deepEqual(tombstone.items[0].job_ref, f.deletedRef);
 const corrupt = structuredClone(f.economics);
 corrupt.totals.api_cost_usd.total = -1;
 assert.throws(() => client.decodeSelectedEconomics(corrupt, f.ref));
})().catch(e => { console.error(e); process.exitCode=1; });
''')
            for store in self.stores():
                job=store.create_job('typescript')
                store.create_job('page2')
                self.usage(store,job)
                economics=self.freeze_job(store,job)
                deleted_job=store.create_job('deleted TypeScript job')
                deleted_ref=store.job_ref(deleted_job.id)
                checkpoint=store.read_job_summary_changes().revision
                store.delete_job(deleted_job.id)
                fixture=Path(tmp)/'fixture.json'
                fixture.write_text(json.dumps(dict(deletedRef=deleted_ref.as_dict(), checkpoint=checkpoint, economics=to_jsonable(economics), ref=economics.job_ref.as_dict(),
                    page=to_jsonable(store.list_job_summaries()),client=dict(python=sys.executable,stateDir=str(store.root),
                    backend=store.backend_name,cwd=str(Path.cwd())))))
                result=subprocess.run([node,str(script),str(fixture)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_real_legacy_projection_layout_upgrade(self):
        from puppetmaster.projections import connection
        from puppetmaster.job_display import FIELDS
        for version in (5,6):
            for store in self.stores():
                job=store.create_job('migration goal')
                ref=store.job_ref(job.id)
                with connection(store) as c:
                    secret=c.execute("SELECT value FROM projection_meta WHERE key='secret'").fetchone()[0]
                    for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                        c.execute('DROP TRIGGER "'+row[0]+'"')
                    for table in ('projection_current','projection_changes','projection_versions'):
                        for field in FIELDS:
                            c.execute('ALTER TABLE '+table+' DROP COLUMN '+field)
                    c.execute('DROP TABLE selected_economics_current')
                    c.execute("DELETE FROM projection_meta WHERE key='display_economics_version'")
                    if store.backend_name=='sqlite':
                        c.execute("UPDATE metadata SET value=? WHERE key='schema_version'",(str(version),))
                reader=type(store)(store.root)
                old=reader.list_job_summaries()
                self.assertEqual(old.outcome,'complete')
                self.assertIsNone(old.items[0].goal_preview)
                self.assertEqual(reader.get_selected_economics(ref).reason,'projection_missing')
                reader.init()
                self.assertEqual(reader.incarnation,ref.incarnation)
                self.assertEqual(reader.list_job_summaries().items[0].goal_preview,'migration goal')
                with connection(reader) as c:
                    self.assertEqual(c.execute("SELECT value FROM projection_meta WHERE key='secret'").fetchone()[0],secret)
                    for table in ('projection_current','projection_changes','projection_versions'):
                        columns={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
                        self.assertTrue(set(FIELDS)<=columns)

    def test_foreign_receipt_and_oversized_projection_fail_closed(self):
        from puppetmaster.projections import connection
        from puppetmaster.models import to_jsonable
        import copy
        for store in self.stores():
            owner=store.create_job('owner')
            self.usage(store,owner)
            frozen=self.freeze_job(store,owner)
            other=store.create_job('other')
            receipt=copy.deepcopy(store.get_job(owner.id).cost_receipt)
            store.save_job(replace(other,status=JobStatus.COMPLETE,cost_receipt=receipt))
            result=store.get_selected_economics(store.job_ref(other.id))
            self.assertEqual(result.reason,'metadata_invalid')
            self.assertIsNone(result.totals)
            with connection(store) as c:
                c.execute('PRAGMA ignore_check_constraints=ON')
                c.execute("UPDATE selected_economics_current SET payload=? WHERE job_id=?",('x'*1000000,owner.id))
            self.assertEqual(store.get_selected_economics(frozen.job_ref).reason,'metadata_invalid')

    def test_active_writer_locks_and_read_side_effects(self):
        import time
        from puppetmaster.projections import connection
        from contextlib import closing
        import sqlite3
        for store in self.stores():
            job=store.create_job('locked')
            self.usage(store,job)
            frozen=self.freeze_job(store,job)
            before={str(p):p.stat().st_mtime_ns for p in store.root.rglob('*') if p.is_file()}
            self.assertEqual(store.get_selected_economics(frozen.job_ref),frozen)
            after={str(p):p.stat().st_mtime_ns for p in store.root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            database=store.root/('state.sqlite3' if store.backend_name=='sqlite' else 'metadata.sqlite3')
            with closing(sqlite3.connect(str(database))) as blocker:
                blocker.execute('BEGIN EXCLUSIVE')
                blocker.execute("UPDATE selected_economics_current SET payload=payload WHERE job_id=?",(job.id,))
                start=time.monotonic()
                result=type(store)(store.root).get_selected_economics(frozen.job_ref)
                self.assertLess(time.monotonic()-start,6)
                if result.outcome=='available':
                    self.assertEqual(result,frozen)
                else:
                    self.assertEqual(result.reason,'read_snapshot_unavailable')
                    self.assertIsNone(result.totals)
                blocker.rollback()
            self.assertEqual(store.get_selected_economics(frozen.job_ref),frozen)

    def test_same_path_replacement_between_validation_and_use(self):
        from unittest.mock import patch
        from puppetmaster.identity import validate, StoreIdentityError
        for store in self.stores():
            job=store.create_job('replace')
            ref=store.job_ref(job.id)
            baseline = store.get_selected_economics(ref)
            protected = []
            def replace_store():
                root = store.root
                root.rename(root.parent / 'previous')
                type(store)(root).create_job('replacement')
            def replace_after_validation(current, selected, c, **kwargs):
                validate(current, selected, c, **kwargs)
                protected.append(replacement_blocked(replace_store))
            with patch('puppetmaster.identity.validate', side_effect=replace_after_validation):
                if sys.platform == 'win32':
                    self.assertEqual(store.get_selected_economics(ref), baseline)
                    self.assertEqual(protected, [True])
                else:
                    with self.assertRaises(StoreIdentityError):
                        store.get_selected_economics(ref)
            if protected == [True]:
                replace_store()
            with self.assertRaises(StoreIdentityError):
                store.get_selected_economics(ref)

    def test_change_rows_match_current_after_other_entities(self):
        from puppetmaster.models import Task, to_jsonable
        for store in self.stores():
            first=store.create_job('first')
            for n in range(5):
                task=Task(job_id=first.id,role='test',instruction='private')
                store.save_task(task)
                store.save_task(replace(task,generation=n+1))
            before=to_jsonable(store.read_job_summary_changes())
            second=store.create_job('second')
            after=store.read_job_summary_changes()
            self.assertEqual(to_jsonable(after)['items'][:len(before['items'])],before['items'])
            current=next(r for r in store.list_job_summaries().items if r.id==second.id)
            change=next(r for r in after.items if r.id==second.id)
            for field in ('goal_preview','goal_preview_truncated','delivery','quality','task_count','artifact_count'):
                self.assertEqual(getattr(current,field),getattr(change,field),field)
