"""Concurrent metadata readers serialize helper startup and session ownership."""
from concurrent.futures import ThreadPoolExecutor
import gc
import os
import signal
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sqlite3
from unittest.mock import patch

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class ConcurrentReuseTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX fork')
    def test_fork_with_locked_registry_uses_fresh_helper(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            held = threading.Event()
            release = threading.Event()

            def hold_registry():
                with readonly._reuse_lock:
                    held.set()
                    release.wait(timeout=15)

            with readonly.connect(store, reuse=True) as parent:
                parent.close()
                thread = threading.Thread(target=hold_registry)
                thread.start()
                child = None
                read_fd, write_fd = os.pipe()
                try:
                    self.assertTrue(held.wait(timeout=5))
                    child = os.fork()
                    if child == 0:
                        os.close(read_fd)
                        signal.alarm(8)
                        try:
                            with readonly.connect(store, reuse=True) as connection:
                                self.assertNotEqual(connection.process.pid, parent.process.pid)
                                self.assertEqual(connection.execute('SELECT 42').fetchone()[0], 42)
                            with self.assertRaises(readonly.ReadUnavailable):
                                parent.execute('SELECT 99')
                            with self.assertRaises(readonly.ReadUnavailable):
                                parent.set_trace_callback(lambda sql: None)
                            parent._abort()
                            parent.close()
                            store._readonly_transport.close()
                            os.write(write_fd, b'ok')
                        except BaseException as exc:
                            os.write(write_fd, repr(exc).encode()[:2000])
                        finally:
                            os._exit(0)
                    os.close(write_fd)
                    write_fd = None
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        waited, status = os.waitpid(child, os.WNOHANG)
                        if waited:
                            child = None
                            break
                        time.sleep(.01)
                    self.assertIsNone(child, 'child deadlocked after fork')
                    self.assertEqual(status, 0, 'child timed out or crashed')
                    self.assertEqual(os.read(read_fd, 2000), b'ok')
                finally:
                    release.set()
                    thread.join(timeout=5)
                    if child:
                        os.kill(child, signal.SIGKILL)
                        os.waitpid(child, 0)
                    os.close(read_fd)
                    if write_fd is not None:
                        os.close(write_fd)
            with readonly.connect(store, reuse=True) as again:
                self.assertIs(again.transport, parent.transport)
                self.assertEqual(again.execute('SELECT 8').fetchone()[0], 8)

    def test_concurrent_start_shares_one_helper_across_store_instances(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as other_root:
            SQLiteSwarmStore(root).ensure_schema()
            other = SQLiteSwarmStore(other_root)
            other.ensure_schema()
            stores = [SQLiteSwarmStore(root) for _ in range(8)]
            barrier = threading.Barrier(len(stores))
            starting = threading.Event()
            proceed = threading.Event()
            original_slot = readonly._reuse_slot
            original_transport = readonly._Transport
            transports = []

            def slot(store, path):
                result = original_slot(store, path)
                if store in stores:
                    barrier.wait(timeout=5)
                return result

            def transport(path):
                if path.parent == stores[0].root:
                    starting.set()
                    if not proceed.wait(timeout=5):
                        raise AssertionError('startup never released')
                    result = original_transport(path)
                    transports.append(result)
                    return result
                return original_transport(path)

            def read(store):
                with readonly.connect(store, reuse=True) as connection:
                    return connection.execute('SELECT 42').fetchone()[0]

            with patch.object(readonly, '_reuse_slot', slot), \
                    patch.object(readonly, '_Transport', transport), \
                    ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(read, store) for store in stores]
                try:
                    self.assertTrue(starting.wait(timeout=5))
                    # Another key progresses while this key is inside startup.
                    self.assertEqual(read(other), 42)
                finally:
                    proceed.set()
                self.assertEqual([future.result(timeout=10) for future in futures], [42] * 8)
            self.assertEqual(len(transports), 1)
            self.assertTrue(all(store._readonly_transport is transports[0] for store in stores))
            self.assertFalse(transports[0].busy.locked())
            stores.clear()
            gc.collect()
            self.assertTrue(transports[0].closed)

    def test_busy_slot_times_out_without_spawning_or_closing_owner(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            peer = SQLiteSwarmStore(root)
            with readonly.connect(store, reuse=True) as owner:
                with patch.object(readonly, '_Transport', side_effect=AssertionError('second helper')):
                    started = time.monotonic()
                    with self.assertRaises(readonly.ReadTimeout):
                        readonly.connect(peer, reuse=True, timeout=.02)
                    self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(owner.transport.closed)
                self.assertEqual(owner.execute('SELECT 7').fetchone()[0], 7)
            with readonly.connect(peer, reuse=True) as next_reader:
                self.assertIs(next_reader.transport, owner.transport)

    def test_waiting_for_shared_helper_does_not_reset_contention_budget(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            with readonly.connect(store, reuse=True):
                pass
            clock = [0.0]
            lock = store._readonly_slot.busy

            class DelayedLock:
                def acquire(self, **kwargs):
                    clock[0] += .08
                    return lock.acquire(**kwargs)

                def release(self):
                    lock.release()

            def sleep(seconds):
                clock[0] += seconds

            timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
            with patch.object(store._readonly_slot, 'busy', DelayedLock()), \
                    patch.object(readonly, 'time', timer), \
                    patch.object(readonly.ReadConnection, '_receive',
                                 side_effect=sqlite3.OperationalError('database is locked')):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'database is locked'):
                    readonly.connect(store, reuse=True)
            self.assertAlmostEqual(clock[0], .1)
            self.assertFalse(lock.locked())

    def test_failed_start_releases_slot_and_failed_response_reaps_once(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            with patch.object(readonly, '_Transport', side_effect=OSError('spawn failed')):
                with self.assertRaisesRegex(OSError, 'spawn failed'):
                    readonly.connect(store, reuse=True)
            self.assertIsNone(store._readonly_slot.transport)
            self.assertFalse(store._readonly_slot.busy.locked())
            original_close = readonly._Transport.close
            with patch.object(readonly._Transport, 'close', autospec=True,
                              side_effect=original_close) as close:
                with readonly.connect(store, reuse=True) as connection:
                    old = connection.transport
                with patch.object(readonly.ReadConnection, '_receive', side_effect=readonly.ReadTimeout('failed')):
                    with self.assertRaises(readonly.ReadTimeout):
                        readonly.connect(store, reuse=True)
                close.assert_called_once_with(old)
                self.assertIsNone(store._readonly_slot.transport)
                self.assertFalse(store._readonly_slot.busy.locked())
                with readonly.connect(store, reuse=True) as replacement:
                    self.assertIsNot(replacement.transport, old)
                close.assert_called_once_with(old)
            self.assertIsNotNone(old.process.poll())


if __name__ == '__main__':
    unittest.main()
