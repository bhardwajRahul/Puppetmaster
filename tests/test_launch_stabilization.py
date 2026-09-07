"""Launch-only stabilization against real SQLite writers and fenced sources."""
import json
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster import identity, mcp_server, readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class LaunchStabilizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'state'
        self.store = SQLiteSwarmStore(self.root)
        self.job = self.store.create_job('launch')
        self.incarnation = self.store._incarnation

    def bind(self, job_id=None):
        return identity.reference_at(self.root, job_id or self.job.id,
                                     expected_incarnation=self.incarnation, launch_binding=True)

    def writer(self, *, hidden=False):
        c = sqlite3.connect(self.store.db_path, check_same_thread=False)
        c.execute("INSERT INTO jobs(id,data) VALUES('concurrent','{}')")
        c.commit()
        pairs = [(Path(str(self.store.db_path) + s), self.root / ('hidden' + s))
                 for s in ('-wal', '-shm')]
        if hidden:
            for source, target in pairs:
                source.rename(target)
        def close():
            if hidden:
                for source, target in pairs:
                    target.rename(source)
            c.close()
        return close

    def test_real_writer_and_missing_sidecars_stabilize(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                close = self.writer(hidden=hidden)
                timer = threading.Timer(.4, close)
                timer.start()
                try:
                    ref = self.bind('concurrent')
                    self.assertEqual(ref.incarnation, self.incarnation)
                    self.assertEqual(ref.job_id, 'concurrent')
                finally:
                    timer.join()
                with closing(sqlite3.connect(self.store.db_path)) as c, c:
                    c.execute("DELETE FROM jobs WHERE id='concurrent'")

    def test_missing_sidecars_timeout_and_no_source_mutation(self):
        close = self.writer(hidden=True)
        def snapshot():
            script = """
import hashlib, json, sys
from pathlib import Path
print(json.dumps({p.name: [p.stat().st_ino, p.stat().st_mtime_ns,
    hashlib.sha256(p.read_bytes()).hexdigest()]
    for p in Path(sys.argv[1]).iterdir() if p.is_file()}, sort_keys=True))
"""
            return subprocess.check_output([sys.executable, '-c', script, str(self.root)])
        before = snapshot()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(readonly.ReadUnavailable, 'active reader|reader timed out'):
                readonly.connect(self.store, timeout=.25, reuse=True, launch_binding=True)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(snapshot(), before)
        finally:
            close()

    def test_wrong_malformed_and_missing_incarnation_and_job(self):
        for value in (str(uuid4()), 'malformed', None):
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.store.db_path)) as c, c:
                    if value is None:
                        c.execute("DELETE FROM metadata WHERE key='incarnation'")
                    else:
                        c.execute("UPDATE metadata SET value=? WHERE key='incarnation'", (value,))
                with self.assertRaises(identity.StoreIdentityError):
                    self.bind()
        with closing(sqlite3.connect(self.store.db_path)) as c, c:
            c.execute("INSERT OR REPLACE INTO metadata VALUES('incarnation',?)", (self.incarnation,))
        with self.assertRaises(KeyError):
            self.bind('absent')

    def test_replacement_during_bind_cannot_return_old_snapshot(self):
        validate = self.store.validate_job_ref
        def replaced(*args, **kwargs):
            ref = validate(*args, **kwargs)
            self.root.rename(self.root.with_name('old'))
            SQLiteSwarmStore(self.root).create_job('replacement')
            return ref
        with patch.object(self.store, 'validate_job_ref', side_effect=replaced):
            with self.assertRaises(identity.StoreIdentityError):
                self.store.job_ref(self.job.id, _launch_binding=True)

    def test_worker_proves_sidecar_change_but_rejects_main_file_aba(self):
        script = """
import runpy, sys
from pathlib import Path
worker = runpy.run_path(sys.argv[1])
main = worker['main']
globals_ = main.__globals__
stamps = globals_['stamps']
path = Path(sys.argv[2])
calls = []
def changed(path):
    calls.append(None)
    if len(calls) == 2:
        if sys.argv[3] == 'sidecar':
            Path(str(path) + '-journal').unlink()
        else:
            moved = path.with_suffix('.old')
            path.rename(moved)
            moved.rename(path)
    return stamps(path)
globals_['stamps'] = changed
main(path)
"""
        worker = Path(readonly.__file__).with_name('readonly_worker.py')
        for change in ('sidecar', 'aba'):
            with self.subTest(change=change):
                Path(str(self.store.db_path) + '-journal').touch()
                result = subprocess.check_output([sys.executable, '-c', script,
                    str(worker), str(self.store.db_path), change], text=True)
                response = json.loads(result)
                self.assertEqual(response['error'], 'unable to open database: source changed')
                self.assertEqual(response['launch_topology_change'], change == 'sidecar')

    def test_launch_uses_fresh_helper_and_reaps_it(self):
        self.store.job_ref(self.job.id)
        cached = self.store._readonly_transport
        self.addCleanup(cached.close)
        with readonly.connect(self.store, reuse=True, launch_binding=True) as c:
            self.assertIsNot(c.transport, cached)
            self.assertEqual(identity.read_identity(c, 'sqlite'), self.incarnation)
        self.assertTrue(c.transport.closed)
        self.assertIsNotNone(c.process.poll())

    def test_missing_source_is_not_created(self):
        root = self.root / 'absent'
        with self.assertRaises((readonly.ReadUnavailable, identity.StoreIdentityError)):
            identity.reference_at(root, self.job.id, expected_incarnation=self.incarnation, launch_binding=True)
        self.assertFalse(root.exists())

    def test_two_simultaneous_launches(self):
        second = self.store.create_job('second')
        barrier = threading.Barrier(2)
        jobs = iter((self.job.id, second.id))
        lock = threading.Lock()
        timers = []
        def reported(*args, **kwargs):
            with lock:
                job_id = next(jobs)
            index = barrier.wait(timeout=5)
            if index == 0:
                release = self.writer(hidden=True)
                timer = threading.Timer(.4, release)
                timers.append(timer)
                self.addCleanup(timer.join)
                timer.start()
            barrier.wait(timeout=5)
            return job_id
        with patch.object(mcp_server.subprocess, 'Popen', side_effect=lambda *a, **k: Mock(pid=987654)), \
                patch.object(mcp_server, '_track_async_process'), \
                patch.object(mcp_server, 'wait_for_job_id', side_effect=reported), \
                patch.object(mcp_server, '_terminate_launcher_tree') as terminate:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(mcp_server.start_cli, ['review', 'goal'],
                           dict(cwd=self.directory.name, state_dir=str(self.root))) for _ in range(2)]
                refs = [json.loads(f.result()['content'][0]['text'])['job_ref'] for f in futures]
            terminate.assert_not_called()
        for timer in timers:
            timer.join()
        self.assertEqual({r['job_id'] for r in refs}, {self.job.id, second.id})
        self.assertEqual({r['incarnation'] for r in refs}, {self.incarnation})


if __name__ == '__main__':
    unittest.main()
