"""Scoped metadata wrappers preserve tombstones and snapshot failure envelopes."""
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from puppetmaster import mcp_server, projections
from puppetmaster.cli import main
from puppetmaster.identity import StoreIdentityError
from puppetmaster.readonly import ReadUnavailable
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class FinalVetoRepairsTests(unittest.TestCase):
    def stores(self):
        for cls in (SwarmStore, SQLiteSwarmStore):
            with TemporaryDirectory() as tmp:
                yield cls(Path(tmp) / 'state')

    def cli(self, store, ref, command='job-summary-changes', **kwargs):
        args = [sys.executable, '-m', 'puppetmaster', '--state-dir', str(store.root),
                '--backend', store.backend_name, '--job-ref', json.dumps(ref), command, '--json']
        for key, value in kwargs.items():
            args.extend(['--' + key.replace('_', '-'), str(value)])
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    def mcp(self, store, ref, command='job-summary-changes', **kwargs):
        handler = {'job-summary-changes': mcp_server.run_job_summary_changes,
                   'job-summaries': mcp_server.run_job_summaries,
                   'selected-economics': mcp_server.run_selected_economics}[command]
        return json.loads(handler(dict(state_dir=str(store.root), backend=store.backend_name,
                                       job_ref=ref, **kwargs))['content'][0]['text'])

    def parity(self, store, ref, **kwargs):
        cli = self.cli(store, ref, **kwargs)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        body = json.loads(cli.stdout)
        self.assertEqual(body, self.mcp(store, ref, **kwargs))
        return body

    def test_live_checkpoint_delete_exact_ref(self):
        for store in self.stores():
            job = store.create_job('tombstone')
            ref = store.job_ref(job.id).as_dict()
            live = self.parity(store, ref)
            self.assertTrue(live['items'])
            self.assertTrue(all(not item['deleted'] for item in live['items']))
            current = self.parity(store, ref, command='job-summaries')
            self.assertEqual(current['items'][0]['id'], job.id)
            checkpoint = live['revision']
            store.delete_job(job.id)
            deleted = self.parity(store, ref, after_revision=checkpoint)
            self.assertEqual([(item['id'], item['deleted']) for item in deleted['items']], [(job.id, True)])
            self.assertEqual(deleted['items'][0]['job_ref'], ref)
            self.assertEqual(self.parity(store, ref, command='job-summaries')['outcome'], 'unavailable')
            with self.assertRaises(KeyError):
                self.mcp(store, ref, command='selected-economics')
            self.assertNotEqual(self.cli(store, ref, command='selected-economics').returncode, 0)

    def test_bad_refs_and_explicit_store_conflicts(self):
        for store in self.stores():
            job = store.create_job('identity')
            ref = store.job_ref(job.id).as_dict()
            store.delete_job(job.id)
            for bad in (dict(ref, incarnation=str(uuid4())), dict(ref, state_id='state_wrong'),
                        dict(job_id=job.id, state_id=ref['state_id']), dict(ref, version=True),
                        dict(ref, incarnation='invalid')):
                with self.subTest(backend=store.backend_name, ref=bad):
                    with self.assertRaises((ValueError, TypeError)):
                        self.mcp(store, bad)
                    self.assertNotEqual(self.cli(store, bad).returncode, 0)
            with self.assertRaises(ValueError):
                self.mcp(store, ref, job_id='other')

    def test_same_path_replacement_aba(self):
        for store in self.stores():
            job = store.create_job('original')
            ref = store.job_ref(job.id).as_dict()
            checkpoint = self.parity(store, ref)['revision']
            store.delete_job(job.id)
            root = store.root
            saved = root.with_name('original')
            root.rename(saved)
            replacement = type(store)(root)
            replacement.create_job('replacement')
            with self.assertRaises(StoreIdentityError):
                self.mcp(replacement, ref, after_revision=checkpoint)
            self.assertNotEqual(self.cli(replacement, ref, after_revision=checkpoint).returncode, 0)
            root.rename(root.with_name('replacement'))
            saved.rename(root)
            self.assertTrue(self.parity(store, ref, after_revision=checkpoint)['items'][0]['deleted'])

    def test_no_ownership_preflight_body_reads_or_writes(self):
        for store in self.stores():
            job = store.create_job('metadata only')
            ref = store.job_ref(job.id).as_dict()
            checkpoint = store.read_job_summary_changes().revision
            store.delete_job(job.id)
            before = {str(p.relative_to(store.root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                      for p in store.root.rglob('*') if p.is_file()}
            original = projections.connection
            statements = []

            @contextmanager
            def guarded(reader, **kwargs):
                self.assertTrue(kwargs.get('metadata_only'))
                with original(reader, **kwargs) as c:
                    execute = c.execute

                    def checked(sql, args=()):
                        statements.append(sql)
                        self.assertNotRegex(sql.lower(), r'\b(insert|update|delete|replace|alter|create)\b')
                        self.assertNotRegex(sql.lower(), r'\b(jobs|tasks|artifacts)\b')
                        return execute(sql, args)
                    c.execute = checked
                    yield c

            with patch('puppetmaster.state.state_owns_job', side_effect=ReadUnavailable('locked')), \
                    patch.object(SwarmStore, 'bind_job_ref', side_effect=AssertionError('bind')), \
                    patch.object(SwarmStore, 'get_job', side_effect=AssertionError('body')), \
                    patch.object(projections, 'connection', guarded):
                self.assertTrue(self.mcp(store, ref, after_revision=checkpoint)['items'][0]['deleted'])
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = main(['--state-dir', str(store.root), '--backend', store.backend_name,
                               '--job-ref', json.dumps(ref), 'job-summary-changes',
                               '--after-revision', str(checkpoint), '--json'])
                self.assertEqual(rc, 0)
                self.assertTrue(json.loads(out.getvalue())['items'][0]['deleted'])
            self.assertTrue(self.parity(store, ref, after_revision=checkpoint)['items'][0]['deleted'])
            after = {str(p.relative_to(store.root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                     for p in store.root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(any('projection_changes' in sql for sql in statements))

    def test_real_exclusive_lock_preserves_checkpoint_and_cursor(self):
        for store in self.stores():
            job = store.create_job('lock')
            ref = store.job_ref(job.id).as_dict()
            first = self.parity(store, ref, limit=1)
            cursor = first['next_cursor']
            checkpoint = first['revision']
            database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
            with sqlite3.connect(str(database), timeout=0) as locked:
                locked.execute('PRAGMA journal_mode=DELETE')
                locked.execute('BEGIN EXCLUSIVE')
                kwargs = dict(after_revision=checkpoint)
                if cursor:
                    kwargs['cursor'] = cursor
                page = self.parity(store, ref, **kwargs)
                self.assertEqual(page['outcome'], 'unavailable')
                self.assertEqual(page['revision'], checkpoint)
                self.assertEqual(page['next_cursor'], cursor)
                self.assertEqual(page['items'], [])
                self.assertIsNotNone(page['retry_after_ms'])
                locked.rollback()
