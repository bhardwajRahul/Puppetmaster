"""Explicit launch binding reads retain bounded helper-start contention retries."""
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster import identity, readonly
from puppetmaster.identity import StoreIdentityError
from puppetmaster.sqlite_store import SQLiteSwarmStore


class ReadonlyLaunchRetryTests(unittest.TestCase):
    def bind(self, errors, *, step=0.2, after_failure=None, query_error=None):
        clock = [0.0]
        failed = []
        opened = []
        receive = readonly.ReadConnection._receive
        self.addCleanup(lambda: [c.close() for c in opened if not c.closed])

        def assert_reaped():
            for c in failed:
                if not c.closed:
                    readonly._cleanup.close(c._token)
                    c.close()
                self.assertTrue(c.closed)
                self.assertIsNotNone(c.process.poll())
                self.assertTrue(c.process.stdin.closed)
                self.assertTrue(c.process.stdout.closed)
                self.assertFalse(c.transport.reader.is_alive())
                self.assertFalse(c.transport.busy.locked())

        def injected(c):
            if c._opened:
                if query_error is not None:
                    failed.append(c)
                    with patch.object(c.responses, 'get', return_value=json.dumps(query_error)):
                        return receive(c)
                return receive(c)
            assert_reaped()
            opened.append(c)
            if errors:
                error = errors.pop(0)
                failed.append(c)
                get = c.responses.get
                def response(timeout):
                    get(timeout=timeout)
                    clock[0] += min(step, timeout)
                    return json.dumps(error)
                with patch.object(c.responses, 'get', response):
                    return receive(c)
            return receive(c)

        def sleep(seconds):
            clock[0] += seconds
            if after_failure:
                after_failure(opened[-1].store)

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp = directory.name
        store = SQLiteSwarmStore(Path(tmp) / 'state')
        job = store.create_job('launch contention')
        with \
                patch.object(readonly, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)), \
                patch.object(readonly.ReadConnection, '_receive', injected):
            try:
                ref = identity.reference_at(Path(tmp) / 'state', job.id,
                    expected_incarnation=store._incarnation, launch_binding=True)
                self.assertEqual(ref.job_id, job.id)
                self.assertEqual(ref.version, 2)
                return len(opened), clock[0]
            finally:
                if query_error is not None:
                    self.assertEqual(len(opened), 1)
                assert_reaped()
                for c in opened:
                    c.close()

    def test_busy_and_locked_initialization_then_success(self):
        # Numeric values work on Python 3.9, before sqlite_errorcode constants.
        for code in (5, 6, 261, 517, 262):
            with self.subTest(code=code):
                count, elapsed = self.bind([dict(kind='OperationalError', error='contended', code=code)])
                self.assertEqual(count, 2)
                self.assertGreater(elapsed, .1)
        for message in ('database is locked', 'database table is locked', 'database schema is locked'):
            with self.subTest(message=message):
                count, _ = self.bind([dict(kind='OperationalError', error=message)])
                self.assertEqual(count, 2)

    def test_confirmed_reader_lock_then_success(self):
        count, elapsed = self.bind([dict(kind='unavailable', code=5,
            error='unable to open database: active reader; sidecars may be missing')])
        self.assertEqual(count, 2)
        self.assertGreater(elapsed, .1)

    def test_unclassified_active_reader_then_success(self):
        count, elapsed = self.bind([dict(kind='unavailable',
            error='unable to open database: active reader; sidecars may be missing')])
        self.assertEqual(count, 2)
        self.assertGreater(elapsed, .1)

    def test_proven_topology_change_then_success(self):
        count, _ = self.bind([dict(kind='unavailable',
            error='unable to open database: source changed', launch_topology_change=True)])
        self.assertEqual(count, 2)

    def test_aba_between_launch_attempts_is_rejected(self):
        def aba(store):
            path = store.root / 'state.sqlite3'
            moved = store.root / 'old.sqlite3'
            path.rename(moved)
            moved.rename(path)
        with self.assertRaises(StoreIdentityError):
            self.bind([dict(kind='unavailable',
                error='unable to open database: active reader; sidecars may be missing')], after_failure=aba)

    def test_exhausted_deadline_propagates_and_reaps(self):
        for code in (5, 6, 261, 262):
            with self.subTest(code=code):
                errors = [dict(kind='OperationalError', error='contended', code=code) for _ in range(3)]
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.bind(errors, step=2.5)
                self.assertEqual(caught.exception.sqlite_errorcode, code)
                self.assertEqual(len(errors), 1)

    def test_non_lock_errors_are_not_retried(self):
        for kind, message, code in (
                ('unavailable', 'unable to open database: active reader; sidecars may be missing', 11),
                ('unavailable', 'unable to open database: source changed', None),
                ('unavailable', 'unable to open database: reader timed out', None),
                ('unavailable', 'unable to open database: source missing', None),
                ('unavailable', 'unable to open database: source changed or unavailable: Permission denied', None),
                ('OperationalError', 'arbitrary failure', None),
                ('OperationalError', 'source changed', None),
                ('OperationalError', 'live sidecars', None),
                ('OperationalError', 'database is locked', 11),
                ('OperationalError', 'no such table: metadata', 1),
                ('DatabaseError', 'database disk image is malformed', 11)):
            with self.subTest(kind=kind, message=message, code=code):
                errors = [dict(kind=kind, error=message, code=code)] * 2
                with self.assertRaises(sqlite3.DatabaseError) as caught:
                    self.bind(errors)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(len(errors), 1)

    def test_identity_change_after_contention_is_not_retried(self):
        def replace_identity(store):
            with closing(sqlite3.connect(store.root / 'state.sqlite3')) as c:
                c.execute("UPDATE metadata SET value='00000000-0000-0000-0000-000000000001' WHERE key='incarnation'")
                c.commit()
        with self.assertRaises(StoreIdentityError):
            self.bind([dict(kind='OperationalError', error='contended', code=5)],
                        after_failure=replace_identity)

    def test_missing_identity_after_contention_is_not_retried(self):
        def remove_identity(store):
            with closing(sqlite3.connect(store.root / 'state.sqlite3')) as c:
                c.execute("DELETE FROM metadata WHERE key='incarnation'")
                c.commit()
        with self.assertRaisesRegex(StoreIdentityError, 'missing or corrupt'):
            self.bind([dict(kind='OperationalError', error='contended', code=262)],
                        after_failure=remove_identity)

    def test_query_lock_is_not_retried(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.bind([], query_error=dict(kind='OperationalError', error='query lock', code=517))
        self.assertEqual(caught.exception.sqlite_errorcode, 517)


if __name__ == '__main__':
    unittest.main()
