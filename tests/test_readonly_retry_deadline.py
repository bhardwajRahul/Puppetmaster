"""A responsive ordinary retry retains its error when its response expires."""
from tests.readonly_fixtures import ProtocolTransport
import io
import json
import queue
import sqlite3
import threading
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class RetryDeadlineTests(unittest.TestCase):
    def _replacement_probe(self, mode, *, locked=False, queued=False, caller=2, replacement_lock=False):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.ensure_schema()
            now = [0.0]
            admissions, waits, transports = [], [], []
            timer = SimpleNamespace(monotonic=lambda: now[0],
                sleep=lambda delay: now.__setitem__(0, now[0] + delay))
            original_admission = readonly.ReaderAdmission
            window = .1 if mode.get('reuse') else 1.0
            expected = (mode.get('attach_deadline', caller)
                        if locked and mode.get('attach_binding') or mode.get('launch_binding')
                        else min(caller, .4 + window))

            def admission(path, deadline, **kwargs):
                admissions.append(deadline)
                if len(admissions) == 1:
                    permit = original_admission(path, deadline, **kwargs)
                    now[0] = .4
                    if locked:
                        permit.release()
                        raise sqlite3.OperationalError('database is locked')
                    return permit
                if queued:
                    now[0] = deadline
                    raise readonly.ReadTimeout('reader timed out')
                return original_admission(path, deadline, **kwargs)

            class Transport(ProtocolTransport):
                def __init__(self, path, deadline=None):
                    super().__init__(path)
                    self.busy = threading.Lock()
                    self.process = SimpleNamespace(stdin=io.StringIO())
                    self.responses = SimpleNamespace(get=self.get)
                    self.closed = False
                    self.calls = 0
                    transports.append(self)

                def get(self, timeout):
                    self.calls += 1
                    waits.append((now[0], timeout))
                    if not locked and self.calls == 1:
                        return json.dumps(dict(session_closed=True, kind='unavailable',
                            error='unable to open database: live sidecars; retry after checkpoint'))
                    if replacement_lock and self.calls == 2:
                        return json.dumps(dict(session_closed=True, kind='OperationalError', error='database is locked', code=5))
                    now[0] += timeout
                    raise queue.Empty()

                def close(self, deadline=None):
                    self.closed = True

            with patch.object(readonly, 'time', timer), \
                    patch.object(readonly, 'ReaderAdmission', admission), \
                    patch.object(readonly, '_Transport', Transport):
                with self.assertRaises(readonly.ReadTimeout):
                    readonly.connect(store, timeout=caller, **mode)
            self.assertEqual(len(admissions), 3 if replacement_lock else 2)
            self.assertAlmostEqual(admissions[1], expected)
            self.assertAlmostEqual(now[0], expected)
            self.assertEqual(len(transports), 2 if locked else 1)
            if not queued:
                self.assertAlmostEqual(sum(waits[-1]), expected)
            self.assertTrue(all(t.closed and not t.busy.locked() for t in transports))

    def test_warm_refresh_preserves_failure_transition_and_deadline(self):
        for outcome in ('accepted', 'exhausted', 'refresh'):
            with self.subTest(outcome=outcome), TemporaryDirectory() as root:
                store = SQLiteSwarmStore(root)
                store.ensure_schema()
                now = [0.0]
                waits, admissions, transports, errors = [], [], [], []
                timer = SimpleNamespace(monotonic=lambda: now[0],
                    sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                original_admission = readonly.ReaderAdmission
                original_receive = readonly.ReadConnection._receive
                def admission(path, deadline, **kwargs):
                    admissions.append(deadline)
                    return original_admission(path, deadline, **kwargs)
                def receive(connection):
                    try:
                        return original_receive(connection)
                    except sqlite3.OperationalError as exc:
                        errors.append(exc)
                        raise
                class Transport(ProtocolTransport):
                    def __init__(self, path, deadline=None):
                        super().__init__(path)
                        self.busy = threading.Lock()
                        self.process = SimpleNamespace(stdin=io.StringIO())
                        self.responses = SimpleNamespace(get=self.get)
                        self.closed = False
                        self.calls = 0
                        transports.append(self)
                    def get(self, timeout):
                        self.calls += 1
                        waits.append((now[0], timeout))
                        if len(transports) == 1:
                            if self.calls <= 2:
                                return json.dumps(dict(journal='delete'))
                            if self.calls == 3:
                                return json.dumps(dict(session_closed=True, kind='OperationalError',
                                    error='database is locked', code=5))
                            # An exhausted write race preserves the previous lock
                            # error; an accepted write retry becomes retry_error.
                            if self.calls == 5 and outcome == 'refresh':
                                now[0] = .08
                                return json.dumps(dict(session_closed=True, kind='unavailable',
                                    error='unable to open database: source changed'))
                            if self.calls > 4:
                                now[0] += timeout
                                raise queue.Empty()
                            now[0] = .1 if outcome == 'exhausted' else .06
                            return json.dumps(dict(session_closed=True, kind='unavailable',
                                error='unable to open database: source changed',
                                same_store_write=True))
                        now[0] += timeout
                        raise queue.Empty()
                    def close(self, deadline=None):
                        self.closed = True
                with patch.object(readonly, 'time', timer), \
                        patch.object(readonly, '_Transport', Transport), \
                        patch.object(readonly, 'ReaderAdmission', admission), \
                        patch.object(readonly.ReadConnection, '_receive', receive):
                    with readonly.connect(store, reuse=True):
                        pass
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        readonly.connect(store, reuse=True, timeout=2)
                self.assertAlmostEqual(now[0], .1)
                self.assertEqual(str(errors[0]), 'database is locked')
                self.assertTrue(errors[1].same_store_write)
                self.assertNotIsInstance(caught.exception, readonly.ReadTimeout)
                self.assertIs(caught.exception, errors[0] if outcome == 'exhausted' else errors[1])
                self.assertTrue(all(t.closed for t in transports))
                self.assertEqual(len(transports), 2 if outcome == 'refresh' else 1)
                self.assertEqual(len(admissions), {'accepted': 4, 'exhausted': 3, 'refresh': 5}[outcome])
                if outcome == 'refresh':
                    self.assertAlmostEqual(admissions[-1], .1)
                    self.assertLessEqual(sum(waits[-1]), .1)

    def test_ordinary_replacement_admission_and_first_response_share_window(self):
        for reuse in (False, True):
            for locked in (False, True):
                for queued in (False, True):
                    for caller in (2, .47):
                        with self.subTest(reuse=reuse, locked=locked, queued=queued, caller=caller):
                            self._replacement_probe(dict(reuse=reuse), locked=locked,
                                                    queued=queued, caller=caller)

    def test_attach_unavailable_replacement_is_short_but_lock_is_aggregate(self):
        for reuse in (False, True):
            for locked in (False, True):
                for queued in (False, True):
                    with self.subTest(reuse=reuse, locked=locked, queued=queued):
                        self._replacement_probe(dict(reuse=reuse, attach_binding=True,
                                                     attach_deadline=4), locked=locked, queued=queued)

    def test_launch_transient_replacement_keeps_caller_window(self):
        for locked in (False, True):
            for queued in (False, True):
                with self.subTest(locked=locked, queued=queued):
                    self._replacement_probe(dict(launch_binding=True), locked=locked, queued=queued)

    def test_attach_replacement_lock_cannot_extend_unavailable_window(self):
        self._replacement_probe(dict(attach_binding=True, attach_deadline=4), replacement_lock=True)

    def test_constructor_boundary_window_is_capped_by_caller_deadline(self):
        for mode in ({}, {'reuse': True}, {'attach_binding': True},
                     {'attach_binding': True, 'attach_deadline': .03}):
            for locked in (False, True):
                with self.subTest(mode=mode, locked=locked), TemporaryDirectory() as root:
                    store = SQLiteSwarmStore(root)
                    store.ensure_schema()
                    now = [0.0]
                    error = (sqlite3.OperationalError('database is locked') if locked else
                             readonly.ReadUnavailable('unable to open database: live sidecars'))
                    timer = SimpleNamespace(monotonic=lambda: now[0],
                        sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                    with patch.object(readonly, 'ReadConnection', side_effect=error), \
                            patch.object(readonly, 'time', timer):
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            readonly.connect(store, timeout=.07, **mode)
                    self.assertIs(caught.exception, error)
                    self.assertAlmostEqual(now[0], mode.get('attach_deadline', .07))

    def test_accepted_retry_timeout_preserves_error_only_for_ordinary(self):
        for mode in ({}, {'reuse': True}, {'attach_binding': True}, {'launch_binding': True}):
            with self.subTest(mode=mode), TemporaryDirectory() as root:
                store = SQLiteSwarmStore(root)
                store.ensure_schema()
                now = [0.0]
                transports = []
                error = readonly.ReadUnavailable('unable to open database: source changed')
                error.same_store_write = True
                class Transport(ProtocolTransport):
                    def __init__(self, path, deadline=None):
                        super().__init__(path)
                        self.busy = threading.Lock()
                        self.process = SimpleNamespace(stdin=io.StringIO())
                        self.responses = SimpleNamespace(get=self.get)
                        self.closed = False
                        self.calls = 0
                        transports.append(self)

                    def get(self, timeout):
                        self.calls += 1
                        if self.calls == 1:
                            return json.dumps(dict(session_closed=True, kind='unavailable',
                                error=str(error), same_store_write=True))
                        now[0] += timeout
                        raise queue.Empty()

                    def close(self, deadline=None):
                        self.closed = True
                timer = SimpleNamespace(monotonic=lambda: now[0],
                    sleep=lambda delay: now.__setitem__(0, now[0]+delay))
                ordinary = not (mode.get('attach_binding') or mode.get('launch_binding'))
                with patch.object(readonly, '_Transport', Transport), patch.object(readonly, 'time', timer):
                    with self.assertRaises(readonly.ReadUnavailable) as caught:
                        readonly.connect(store, timeout=2, **mode)
                self.assertEqual(isinstance(caught.exception, readonly.ReadTimeout), not ordinary)
                self.assertEqual(now[0], (.1 if mode.get('reuse') else 1) if ordinary else 2)
                self.assertEqual(len(transports), 1)
                self.assertTrue(transports[0].closed)
                self.assertFalse(transports[0].busy.locked())
