"""Transient reads at child binding and CLI polling boundaries."""
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from puppetmaster import readonly
from puppetmaster.cli import commands_jobs
from puppetmaster.identity import StoreIdentityError
from puppetmaster.models import JobStatus
from puppetmaster.sqlite_store import SQLiteSwarmStore


class PollAvailabilityTests(unittest.TestCase):
    def test_transient_state_and_event_reads_retry_under_one_deadline(self):
        now = [0.0]
        observed = []
        timer = SimpleNamespace(monotonic=lambda: now[0],
            sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        pending = dict(status='running', terminal=False, timed_out=False)
        complete = dict(status='complete', terminal=True, timed_out=False)
        responses = iter([readonly.ReadUnavailable('active reader'),
                          readonly.ReadTimeout('reader timed out'), pending, complete])
        def state(*args, **kwargs):
            observed.append(readonly._read_deadline.get())
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        store = SimpleNamespace(wait_for_events=lambda *args, **kwargs: (_ for _ in ()).throw(
            readonly.ReadUnavailable('active reader')))
        with patch.object(commands_jobs, 'time', timer), \
                patch.object(commands_jobs, 'read_job_state', state):
            result = commands_jobs.await_job_state(store, 'job', timeout_seconds=1,
                                                  poll_interval_seconds=.1)
        self.assertEqual(result, complete)
        self.assertEqual(observed, [1.0] * 4)
        self.assertAlmostEqual(now[0], .3)
        self.assertIsNone(readonly._read_deadline.get())

    def test_transient_timeout_returns_timed_out_and_identity_is_not_retried(self):
        now = [0.0]
        timer = SimpleNamespace(monotonic=lambda: now[0],
            sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        with patch.object(commands_jobs, 'time', timer), \
                patch.object(commands_jobs, 'read_job_state', side_effect=readonly.ReadUnavailable('active reader')):
            state = commands_jobs.await_job_state(object(), 'job', timeout_seconds=.12,
                                                 poll_interval_seconds=.1)
        self.assertTrue(state['timed_out'])
        self.assertEqual(now[0], .12)
        for error in (StoreIdentityError('replaced'), sqlite3.DatabaseError('malformed')):
            with patch.object(commands_jobs, 'read_job_state', side_effect=error) as read:
                with self.assertRaises(type(error)):
                    commands_jobs.await_job_state(object(), 'job', timeout_seconds=1)
                read.assert_called_once()

    def test_read_deadline_caps_real_helper_and_restores_context(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            with readonly.connect(store):
                started = time.monotonic()
                with readonly.read_deadline(started + .04):
                    with self.assertRaises(readonly.ReadTimeout):
                        readonly.connect(store, timeout=5)
                self.assertLess(time.monotonic() - started, .2)
            readonly._cleanup.maintain(limit=len(readonly._cleanup.owners))
            self.assertIsNone(readonly._read_deadline.get())

    def test_four_child_bindings_and_awaits_survive_real_writer(self):
        script = r'''
import sys
from puppetmaster import projections
from puppetmaster.cli import main
connection = projections.connection
def observed(*args, **kwargs):
    if kwargs.get('launch_binding'):
        print('binding', flush=True)
    return connection(*args, **kwargs)
projections.connection = observed
raise SystemExit(main(sys.argv[1:]))
'''
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            jobs = [store.create_job('availability fixture') for _ in range(4)]
            for job in jobs:
                store.update_job_status(job.id, JobStatus.CANCELLED)
                summary = store.job_dir(job.id) / 'summaries' / 'stitched.md'
                summary.parent.mkdir(parents=True, exist_ok=True)
                summary.write_text('fixture complete', encoding='utf-8')
            writer = sqlite3.connect(store.db_path)
            writer.execute('BEGIN EXCLUSIVE')
            writer.execute("UPDATE metadata SET value=value WHERE key='incarnation'")
            processes = []
            try:
                for job in jobs:
                    processes.append(subprocess.Popen([sys.executable, '-c', script,
                        '--state-dir', root, '--backend', 'sqlite',
                        '--store-incarnation', store._incarnation,
                        'await', job.id, '--timeout-seconds', '5', '--json'],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                for process in processes:
                    self.assertEqual(process.stdout.readline().strip(), 'binding')
                time.sleep(.1)
                writer.rollback()
                writer.close()
                for process in processes:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertNotIn('Traceback', stderr)
                    self.assertEqual(process.returncode, 1, stderr)
                    payload = json.loads(stdout)
                    self.assertEqual(payload['status'], 'cancelled')
                    self.assertTrue(payload['terminal'])
                    self.assertFalse(payload['timed_out'])
            finally:
                writer.close()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
