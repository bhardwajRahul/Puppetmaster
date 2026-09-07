"""Attach contention must not turn lock retries into interpreter startup storms."""
import hashlib
import json
import sqlite3
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.identity import StoreIdentityError


class NoAdmission:
    """Exercise the helper BUSY protocol independently of production admission."""
    def __init__(self, *args, **kwargs):
        pass

    def release(self):
        pass


class AttachReaderContentionTests(unittest.TestCase):
    @patch.object(readonly, 'ReaderAdmission', NoAdmission)
    def test_concurrent_attaches_retry_without_respawning_or_mutation(self):
        with TemporaryDirectory() as root:
            supervisor = SQLiteSwarmStore(root)
            supervisor.ensure_schema()
            def snapshot():
                return {p.name: (p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.stat().st_ctime_ns,
                                 hashlib.sha256(p.read_bytes()).hexdigest())
                        for p in Path(root).iterdir() if p.is_file()}
            before = snapshot()
            count = 32
            barrier = threading.Barrier(count)
            collided = set()
            mutex = threading.Lock()
            all_collided = threading.Event()
            receive = readonly.ReadConnection._receive
            def observed(connection):
                try:
                    return receive(connection)
                except Exception as exc:
                    if readonly._locked(exc):
                        with mutex:
                            collided.add(threading.get_ident())
                            if len(collided) == count:
                                all_collided.set()
                    raise
            def attach():
                store = SQLiteSwarmStore(root)
                barrier.wait(timeout=10)
                store.attach()
                return store._incarnation
            # The helper holds the advisory lock; every contender must collide
            # before release, independently of scheduler timing.
            held = readonly.connect(supervisor)
            started = time.monotonic()
            with patch.object(readonly, '_Transport', wraps=readonly._Transport) as spawn, \
                    patch.object(readonly.ReadConnection, '_receive', observed), \
                    ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(attach) for _ in range(count)]
                try:
                    self.assertTrue(all_collided.wait(10), len(collided))
                finally:
                    held.close()
                self.assertEqual([f.result(timeout=15) for f in futures],
                                 [supervisor._incarnation] * count)
                self.assertEqual(spawn.call_count, count)
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(snapshot(), before)

    @patch.object(readonly, 'ReaderAdmission', NoAdmission)
    def test_replacement_between_lock_retries_is_rejected(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(Path(root) / 'state')
            store.ensure_schema()
            receive = readonly.ReadConnection._receive
            replaced = []
            def observed(connection):
                try:
                    return receive(connection)
                except sqlite3.OperationalError:
                    if not replaced:
                        held.close()
                        # Wait for the failed helper to release its source fd.
                        connection.process.stdin.write(json.dumps({'open': str(store.db_path)}) + '\n')
                        connection.process.stdin.flush()
                        receive(connection)
                        connection._control('release', True)
                        store.root.rename(Path(root) / 'old')
                        SQLiteSwarmStore(store.root).ensure_schema()
                        replaced.append(True)
                    raise
            with readonly.connect(store) as held, \
                    patch.object(readonly.ReadConnection, '_receive', observed), \
                    patch.object(readonly, '_Transport', wraps=readonly._Transport) as spawn:
                with self.assertRaises(StoreIdentityError):
                    readonly.connect(store, attach_binding=True)
                self.assertEqual(spawn.call_count, 1)
            self.assertTrue(replaced)

    @patch.object(readonly, 'ReaderAdmission', NoAdmission)
    def test_persistent_lock_is_bounded_and_helper_reaped(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            with readonly.connect(store):
                processes = []
                spawn = readonly._Transport
                def tracked(*args, **kwargs):
                    process = spawn(*args, **kwargs)
                    processes.append(process.process)
                    return process
                started = time.monotonic()
                with patch.object(readonly, '_Transport', tracked):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'database is locked|active reader|reader timed out'):
                        readonly.connect(store, timeout=.3, attach_binding=True)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(len(processes), 1)
                readonly._cleanup.maintain(limit=len(readonly._cleanup.owners))
                self.assertTrue(all(p.poll() is not None for p in processes))

    def test_replacement_while_queued_is_rejected(self):
        for aba in (False, True, 'directory'):
            with self.subTest(aba=aba), TemporaryDirectory() as root:
                store = SQLiteSwarmStore(Path(root) / 'state')
                store.ensure_schema()
                queued = threading.Event()
                proceed = threading.Event()
                admission = readonly.ReaderAdmission
                def observed(*args, **kwargs):
                    queued.set()
                    self.assertTrue(proceed.wait(5))
                    return admission(*args, **kwargs)
                with readonly.connect(store) as held, \
                        patch.object(readonly, 'ReaderAdmission', observed), \
                        patch.object(readonly, '_Transport', wraps=readonly._Transport) as spawn, \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(readonly.connect, store, attach_binding=True)
                    try:
                        self.assertTrue(queued.wait(5))
                        held.close()
                        if aba == 'directory':
                            moved = Path(root) / 'moved'
                            store.root.rename(moved)
                            moved.rename(store.root)
                        elif aba:
                            moved = store.root / 'moved'
                            store.db_path.rename(moved)
                            moved.rename(store.db_path)
                        else:
                            store.root.rename(Path(root) / 'old')
                            SQLiteSwarmStore(store.root).ensure_schema()
                    finally:
                        proceed.set()
                    with self.assertRaises(StoreIdentityError):
                        future.result(timeout=5)
                    self.assertEqual(spawn.call_count, 1)


if __name__ == '__main__':
    unittest.main()
