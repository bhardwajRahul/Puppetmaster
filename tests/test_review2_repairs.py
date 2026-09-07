"""Adversarial regressions from the independent v1.25 release review."""
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from test_marionette_attach_blockers import fingerprint, locked_fingerprint
from test_v1250_release_blockers import measured_reads
from puppetmaster.attempts import ExecutionAttempt
from puppetmaster.contracts import CursorCodec, TaskBinding, bounded_evidence
from puppetmaster.identity import StoreIdentityError
from puppetmaster.models import JobRef
from puppetmaster.projections import connection
from puppetmaster.readonly import ReadUnavailable, connect
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.state import resolve_job_state, state_owns_job
from puppetmaster.store import SwarmStore
from puppetmaster.store_contracts import StoreContracts
from puppetmaster.store_factory import create_store


class Review2Repairs(unittest.TestCase):
    def stores(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with TemporaryDirectory() as tmp:
                store = cls(Path(tmp) / 'state')
                job = store.create_job('review proof')
                ref = store.job_ref(job.id)
                store.record_attempt(ExecutionAttempt(job.id, 't', 'r1', 'a1', 'now', 'codex'))
                store.record_attempt(ExecutionAttempt(job.id, 't', 'r2', 'a2', 'now', 'codex'))
                yield store, job, ref

    def test_exact_generator_consumption_before_sort_or_transaction(self):
        fake = StoreContracts()
        fake.validate_job_ref = lambda *args, **kwargs: None
        for operation, make in (
                (lambda values: fake.request_cancellation(None, 'request', values),
                 lambda index: TaskBinding(str(index), 1, 'lease', 'owner')),
                (lambda values: fake.advance_effect(None, 'effect', expected_revision=1,
                                                   outcome='in_flight', evidence_refs=values), str)):
            consumed = []
            def unlimited():
                index = 0
                while True:
                    consumed.append(index)
                    yield make(index)
                    index += 1
            with self.assertRaises(ValueError):
                operation(unlimited())
            self.assertEqual(len(consumed), 201)

    def test_evidence_item_count_and_aggregate_byte_caps(self):
        for values in (['x'] * 201, ['x' * 4097], ['漢' * 1366], ['x' * 4096] * 17):
            with self.assertRaises(ValueError):
                bounded_evidence(iter(values))
        self.assertEqual(len(bounded_evidence(['x' * 4096] * 16)), 16)

    def test_historical_control_scalars_never_cross_python_unbounded(self):
        for store, job, ref in self.stores():
            token = store.list_attempt_refs(ref, limit=1).next_cursor
            for key, value in (('secret', 'f' * 300000), ('secret', 'z' * 64),
                               ('epoch', 'x' * 300000), ('epoch', '-1')):
                with self.subTest(backend=store.backend_name, key=key, size=len(value)):
                    with connection(store) as c:
                        prior = c.execute('SELECT value FROM projection_meta WHERE key=?', (key,)).fetchone()[0]
                        c.execute('UPDATE projection_meta SET value=? WHERE key=?', (value, key))
                    with measured_reads(deny_bodies=True) as metrics:
                        page = store.list_attempt_refs(ref, cursor=token)
                    self.assertEqual(page.outcome, 'unavailable')
                    self.assertEqual(page.next_cursor, token)
                    self.assertLess(metrics['bytes'], 2048)
                    with connection(store) as c:
                        c.execute('UPDATE projection_meta SET value=? WHERE key=?', (prior, key))
            for table, column in (('historical_counts', 'count'), ('historical_epochs', 'epoch')):
                for value in ('x' * 300000, -1, 1.5):
                    with connection(store) as c:
                        c.execute(f'INSERT OR REPLACE INTO {table}(kind,job_id,{column}) VALUES(?,?,?)',
                                  ('attempt', job.id, value))
                    with measured_reads(deny_bodies=True) as metrics:
                        page = store.list_attempt_refs(ref)
                        counts = store.historical_evidence_counts(ref)
                    self.assertEqual(page.outcome, 'unavailable')
                    if table == 'historical_counts':
                        self.assertEqual(counts.outcome, 'unavailable')
                    self.assertLess(metrics['bytes'], 2048)
                with connection(store) as c:
                    c.execute(f'UPDATE {table} SET {column}=? WHERE kind=? AND job_id=?',
                              (2 if column == 'count' else 0, 'attempt', job.id))

    def test_malformed_facts_and_receipt_scalars_are_unavailable(self):
        for store, job, ref in self.stores():
            token = store.list_attempt_refs(ref, limit=1).next_cursor
            for raw in ('[]', '{broken', '{"attempt_id":3}'):
                with connection(store) as c:
                    c.execute("UPDATE historical_refs SET facts=? WHERE kind='attempt'", (raw,))
                page = store.list_attempt_refs(ref, cursor=token)
                self.assertEqual((page.outcome, page.next_cursor), ('unavailable', token))
            for digest in ('short', 'x' * 300000, sqlite3.Binary(b'x' * 64)):
                with connection(store) as c:
                    c.execute("INSERT OR REPLACE INTO completion_receipts VALUES(?,?,?,'published')",
                              (job.id, 'r', digest))
                with measured_reads(deny_bodies=True) as metrics:
                    receipt = store.get_completion_receipt(ref, 'r')
                self.assertEqual(receipt.outcome, 'unavailable')
                self.assertIsNone(receipt.intent_digest)
                self.assertLess(metrics['bytes'], 1024)

    def test_projection_integer_affinity_and_control_scalars_are_bounded(self):
        for store, job, ref in self.stores():
            for column in ('revision', 'task_count', 'artifact_count'):
                for value in ('x' * 300000, -1, 1.5):
                    with connection(store) as c:
                        c.execute(f"UPDATE projection_current SET {column}=? WHERE kind='job' AND id=?", (value, job.id))
                    with measured_reads(deny_bodies=True) as metrics:
                        page = store.list_job_summaries(job_ref=ref)
                    self.assertEqual(page.outcome, 'unavailable')
                    self.assertLess(metrics['bytes'], 4096)
                with connection(store) as c:
                    c.execute(f"UPDATE projection_current SET {column}=0 WHERE kind='job' AND id=?", (job.id,))
            with connection(store) as c:
                c.execute("UPDATE projection_meta SET value=? WHERE key='retention_floor'", ('x' * 300000,))
            with measured_reads(deny_bodies=True) as metrics:
                page = store.read_job_summary_changes(after_revision=7)
            self.assertEqual((page.outcome, page.revision), ('unavailable', 7))
            self.assertLess(metrics['bytes'], 4096)

    def test_history_unavailable_preserves_cursor_and_invalid_is_explicit(self):
        for store, job, ref in self.stores():
            token = store.list_attempt_refs(ref, limit=1).next_cursor
            with connection(store) as c:
                c.execute("DELETE FROM projection_meta WHERE key='history_version'")
            self.assertEqual(store.list_attempt_refs(ref, cursor=token).next_cursor, token)
            with self.assertRaises(ValueError):
                store.list_attempt_refs(ref, cursor='malformed')
            with connection(store) as c:
                c.execute("INSERT INTO projection_meta VALUES('history_version','1')")
                c.execute("DELETE FROM projection_current WHERE kind='job' AND id=?", (job.id,))
            page = store.list_attempt_refs(ref, cursor=token)
            self.assertEqual((page.outcome, page.next_cursor), ('unavailable', token))
            with connection(store) as c:
                c.execute("UPDATE projection_meta SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'")
            self.assertEqual(store.list_attempt_refs(ref, cursor=token).outcome, 'cursor_expired')

    def test_emitted_cursor_roundtrips_or_page_is_unavailable_without_advance(self):
        for store, job, ref in self.stores():
            for prefix, size in (('a', 800), ('漢', 600)):
                with connection(store) as c:
                    c.execute("DELETE FROM projection_current WHERE kind='task'")
                    for suffix in ('1', '2'):
                        c.execute("""INSERT INTO projection_current(kind,job_id,id,status,revision,stamp)
                            VALUES('task',?,?,'queued',0,'known')""", (job.id, prefix * size + suffix))
                page = store.list_task_refs(ref, limit=1)
                if prefix == 'a':
                    self.assertEqual(page.outcome, 'partial')
                    self.assertLessEqual(len(page.next_cursor), 4096)
                    self.assertTrue(store.list_task_refs(ref, cursor=page.next_cursor).items)
                else:
                    self.assertEqual(page.outcome, 'unavailable')
                    self.assertIsNone(page.next_cursor)
        with self.assertRaises(ReadUnavailable):
            CursorCodec(b'secret').encode(dict(v=1, scope='s', last='x' * 5000))

    def test_unmigrated_v5_legacy_binding_reads_without_identity_bootstrap(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp) / 'state')
            job = store.create_job('legacy')
            ref = JobRef(job.id, store.job_ref(job.id).state_id)
            with closing(sqlite3.connect(store.db_path)) as c, c:
                c.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
                c.execute("DELETE FROM metadata WHERE key='incarnation'")
            before = fingerprint(store.root)
            attached = create_store('sqlite', store.root, mode='attach')
            attached.bind_job_ref(ref, legacy_read=True)
            self.assertEqual(attached.get_job(job.id), job)
            self.assertEqual(attached.list_job_summaries(job_ref=ref).items[0].job_ref, ref)
            for operation in (lambda: attached.bind_job_ref(ref),
                              lambda: attached.request_cancellation(ref, 'stop', [])):
                with self.assertRaises(StoreIdentityError):
                    operation()
            result = subprocess.run([sys.executable, '-m', 'puppetmaster', '--state-dir', str(store.root),
                '--backend', 'sqlite', '--job-ref', json.dumps(ref.as_dict()),
                'await', job.id, '--json', '--timeout-seconds', '0.001'], capture_output=True, text=True)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            self.assertEqual(json.loads(result.stdout)['job_ref'], ref.as_dict())
            self.assertEqual(fingerprint(store.root), before)

    def test_ownership_resolution_preserves_checkpointed_and_live_sources(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(Path(tmp) / 'state')
            job = store.create_job('owned')
            ref = store.job_ref(job.id)
            for journal in ('delete', 'wal'):
                with closing(sqlite3.connect(store.db_path)) as c:
                    c.execute('PRAGMA journal_mode=' + journal)
                before = fingerprint(store.root)
                with patch('sqlite3.connect', side_effect=AssertionError('source SQLite open in caller')):
                    selected = resolve_job_state(state_dir=str(store.root), job_ref=ref.as_dict())
                self.assertEqual(selected, store.root)
                self.assertEqual(fingerprint(store.root), before)
            with closing(sqlite3.connect(store.db_path)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                before = locked_fingerprint(store.root)
                with self.assertRaises(ReadUnavailable):
                    resolve_job_state(state_dir=str(store.root), job_ref=ref.as_dict())
                self.assertEqual(locked_fingerprint(store.root), before)
                probe = subprocess.run([sys.executable, '-c',
                    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute('BEGIN IMMEDIATE')",
                    str(store.db_path)], capture_output=True, text=True)
                self.assertNotEqual(probe.returncode, 0)
                self.assertIn('locked', probe.stderr)

    def test_retained_checkpointed_wal_is_available_but_unproven_index_is_not(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(tmp)
            store.create_job('checkpointed')
            wal, shm = Path(str(store.db_path) + '-wal'), Path(str(store.db_path) + '-shm')
            with closing(sqlite3.connect(store.db_path)) as writer:
                writer.execute("INSERT INTO metadata VALUES('checkpointed_only','published')")
                writer.commit()
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                saved_wal, saved_shm = wal.read_bytes(), shm.read_bytes()
            # Model SQLite's persistent-sidecar builds on every platform.
            wal.write_bytes(saved_wal)
            shm.write_bytes(saved_shm)
            before = fingerprint(store.root)
            with closing(connect(store, timeout=0)) as reader:
                self.assertEqual(reader.execute("SELECT value FROM metadata WHERE key='checkpointed_only'").fetchone()[0], 'published')
            self.assertTrue(store.list_job_summaries().items)
            self.assertEqual(fingerprint(store.root), before)
            wal.unlink()
            before = fingerprint(store.root)
            self.assertTrue(store.list_job_summaries().items)
            self.assertEqual(fingerprint(store.root), before)
            corrupt = bytearray(saved_shm)
            corrupt[96:100] = (2147483647).to_bytes(4, sys.byteorder)
            shm.write_bytes(corrupt)
            before = fingerprint(store.root)
            with self.assertRaises(ReadUnavailable):
                connect(store, timeout=0)
            self.assertEqual(fingerprint(store.root), before)

    def test_post_launch_reference_failure_reaps_owned_launcher(self):
        from puppetmaster import mcp_server, swarm_launch
        real_popen = subprocess.Popen
        for module in (mcp_server, swarm_launch):
            with TemporaryDirectory() as tmp:
                owned = []
                def spawn(*args, **kwargs):
                    process = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    owned.append(process)
                    return process
                try:
                    with patch.object(module.subprocess, 'Popen', side_effect=spawn), \
                            patch.object(module, 'wait_for_job_id', return_value='job_started'), \
                            patch('puppetmaster.identity.' + ('make_ref' if module is mcp_server else 'reference_at'),
                                  side_effect=StoreIdentityError('post-launch proof')):
                        with self.assertRaisesRegex(StoreIdentityError, 'post-launch proof'):
                            if module is mcp_server:
                                module.start_cli(['cursor', 'review', 'goal'], dict(cwd=tmp, state_dir=str(Path(tmp) / 'state')))
                            else:
                                module.detach_analysis_swarm(goal='goal', roles=['explore'], adapter='codex',
                                    state_dir=Path(tmp) / 'state', cwd=tmp)
                    self.assertEqual(len(owned), 1)
                    self.assertIsNotNone(owned[0].poll())
                finally:
                    for process in owned:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file-size resource limit proof')
    def test_large_source_reads_do_no_snapshot_writes_or_unrelated_scan(self):
        import resource
        from puppetmaster.readonly import ReaderProcess
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(tmp)
            store.create_job('small metadata')
            with store._writer_scope() as c:
                c.execute('CREATE TABLE unrelated_large_body(payload BLOB)')
                c.execute('INSERT INTO unrelated_large_body VALUES(zeroblob(67108864))')
            self.assertGreater(store.db_path.stat().st_size, 67108864)
            before = fingerprint(store.root)
            processes = []
            def no_file_writes(*args, **kwargs):
                kwargs['preexec_fn'] = lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
                process = ReaderProcess(*args, **kwargs)
                processes.append(process)
                return process
            steps = []
            original = connection
            from contextlib import contextmanager
            @contextmanager
            def counted(*args, **kwargs):
                with original(*args, **kwargs) as c:
                    c.set_progress_handler(lambda: steps.append(1) or 0, 100)
                    yield c
            with patch('puppetmaster.readonly.ReaderProcess', side_effect=no_file_writes), \
                    patch('puppetmaster.projections.connection', counted), measured_reads(deny_bodies=True) as metrics:
                page = store.list_job_summaries(limit=1, max_scan=1)
            self.assertEqual(len(page.items), 1)
            self.assertNotEqual(page.outcome, 'unavailable')
            self.assertEqual(metrics['connections'], 1)
            self.assertLess(metrics['bytes'], 4096)
            self.assertGreater(len(steps), 0)
            self.assertLess(len(steps), 60)
            self.assertNotIn(('unrelated_large_body', 'payload'), metrics['reads'])
            self.assertEqual(fingerprint(store.root), before)
            self.assertTrue(all(p.poll() is not None for p in processes))

    def test_reader_timeout_is_bounded_and_reaped(self):
        from puppetmaster.readonly import ReaderProcess
        with TemporaryDirectory() as tmp:
            store = SQLiteSwarmStore(tmp)
            store.create_job('owned')
            owned = []
            def stalled(*args, **kwargs):
                process = ReaderProcess([sys.executable, '-c', 'import time;time.sleep(60)'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8')
                owned.append(process)
                return process
            with patch('puppetmaster.readonly.ReaderProcess', side_effect=stalled):
                with self.assertRaises(ReadUnavailable):
                    connect(store, timeout=0)
            self.assertIsNotNone(owned[0].poll())
            self.assertTrue(owned[0].stdin.closed)
            self.assertTrue(owned[0].stdout.closed)


if __name__ == '__main__':
    unittest.main()
