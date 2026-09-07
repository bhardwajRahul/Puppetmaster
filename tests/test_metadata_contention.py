"""Metadata contention retries acquisition, never a published transaction."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from puppetmaster import projections, readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore


class MetadataContentionTests(unittest.TestCase):
    def test_file_reservation_retries_only_before_body(self):
        for outcome in ('success', 'body_error', 'exhausted', 'corrupt'):
            with self.subTest(outcome=outcome), TemporaryDirectory() as tmp:
                store = SwarmStore(tmp)
                store.init()
                native_connect = sqlite3.connect
                clock = [0.0]
                attempts, effects = [], []
                error = sqlite3.OperationalError('database is locked' if outcome != 'corrupt' else 'corrupt')
                class Connection(sqlite3.Connection):
                    def execute(self, sql, *args):
                        if sql == 'BEGIN IMMEDIATE':
                            attempts.append(sql)
                            if len(attempts) < 3 or outcome in ('exhausted', 'corrupt'):
                                clock[0] += .2
                                raise error
                        return super().execute(sql, *args)
                def connect(*args, **kwargs):
                    return native_connect(*args, **kwargs, factory=Connection)
                def sleep(delay):
                    clock[0] += delay
                def write():
                    with projections.connection(store, write=True) as c:
                        effects.append(True)
                        c.execute("INSERT INTO projection_pending VALUES('test')")
                        if outcome == 'body_error':
                            raise error
                with patch.object(projections.sqlite3, 'connect', connect), patch.object(
                        projections, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)):
                    if outcome == 'success':
                        write()
                    else:
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            write()
                        self.assertIs(caught.exception, error)
                self.assertEqual(len(effects), int(outcome in ('success', 'body_error')))
                if outcome in ('success', 'body_error'):
                    self.assertEqual(len(attempts), 3)
                elif outcome == 'corrupt':
                    self.assertEqual(len(attempts), 1)
                else:
                    self.assertLessEqual(clock[0], 5.21)
                    self.assertGreaterEqual(clock[0], 5)
                with projections.connection(store) as c:
                    self.assertEqual(c.execute("SELECT count(*) FROM projection_pending WHERE path='test'").fetchone()[0],
                                     int(outcome == 'success'))

    def test_ordinary_confirmed_reader_contention_uses_one_bounded_helper(self):
        for outcome in ('success', 'exhausted', 'unproven', 'replacement'):
            with self.subTest(outcome=outcome), TemporaryDirectory() as tmp:
                store = SQLiteSwarmStore(tmp)
                store.init()
                receive = readonly.ReadConnection._receive
                clock = [0.0]
                failed = []
                def injected(c):
                    response = receive(c)
                    if not c._opened and (not failed or outcome == 'exhausted'):
                        # Release the real session before emulating its failed
                        # acquisition response; the helper is ready for open.
                        c._opened = True
                        c._control('release', True)
                        c._opened = False
                        failed.append(c)
                        clock[0] += .1 if outcome == 'unproven' else .01
                        error = dict(kind='unavailable', error='unable to open database: active reader; sidecars may be missing')
                        if outcome != 'unproven':
                            error['code'] = 5
                        with patch.object(c.responses, 'get', return_value=json.dumps(error)):
                            return receive(c)
                    return response
                def sleep(delay):
                    clock[0] += delay
                    if outcome == 'replacement':
                        store.db_path.rename(store.db_path.with_suffix('.old'))
                        store.db_path.write_bytes(b'replacement')
                try:
                    with patch.object(readonly, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)), \
                            patch.object(readonly.ReadConnection, '_receive', injected), \
                            patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn:
                        if outcome == 'success':
                            with readonly.connect(store, reuse=True, timeout=1) as c:
                                self.assertGreater(c.execute('SELECT count(*) FROM metadata').fetchone()[0], 0)
                        else:
                            from puppetmaster.identity import StoreIdentityError
                            with self.assertRaises(StoreIdentityError if outcome == 'replacement' else sqlite3.OperationalError):
                                readonly.connect(store, reuse=True, timeout=1)
                        self.assertEqual(spawn.call_count, 1)
                        self.assertLessEqual(clock[0], .1)
                        if outcome == 'exhausted':
                            self.assertAlmostEqual(clock[0], .1)
                        if outcome != 'success':
                            self.assertTrue(failed[0].closed)
                finally:
                    transport = getattr(store, '_readonly_transport', None)
                    if transport:
                        transport.close()


if __name__ == '__main__':
    unittest.main()
