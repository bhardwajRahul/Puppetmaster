"""An exhausted contention window must not masquerade as silent startup."""
import json
import queue
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class RetryDeadlineTests(unittest.TestCase):
    def test_retry_response_expiry_preserves_only_short_ordinary_contention(self):
        for options, budget, preserve in (
                ({}, 1.0, True),
                ({'reuse': True}, .1, True),
                ({'timeout': .075}, .075, False),
                ({'attach_binding': True, 'timeout': .075}, .075, False),
                ({'launch_binding': True, 'timeout': .075}, .075, False)):
            for write_race in (False, True):
                if options.get('launch_binding') and not write_race:
                    # Launch BUSY uses outer retries, not this response window.
                    continue
                with self.subTest(options=options, write_race=write_race), TemporaryDirectory() as root:
                    store = SQLiteSwarmStore(root)
                    store.ensure_schema()
                    clock = [0.0]
                    receive = readonly.ReadConnection._receive
                    opened = []
                    waits = []
                    errors = []

                    def expired(timeout):
                        waits.append(timeout)
                        clock[0] += timeout
                        raise queue.Empty()

                    def delayed(connection):
                        if opened and not connection._opened:
                            # The helper's retry response is scheduled just
                            # after the remaining contention window expires.
                            with patch.object(connection.responses, 'get', side_effect=expired):
                                return receive(connection)
                        response = receive(connection)
                        if not connection._opened:
                            opened.append(connection)
                            connection._opened = True
                            connection._control('release', True)
                            connection._opened = False
                            clock[0] = budget - (.01 if write_race else .05) - .0001
                            error = (dict(kind='unavailable', error='unable to open database: source changed',
                                          same_store_write=True) if write_race else
                                     dict(kind='OperationalError', error='database is locked', code=5))
                            with patch.object(connection.responses, 'get', return_value=json.dumps(error)):
                                try:
                                    return receive(connection)
                                except sqlite3.OperationalError as exc:
                                    errors.append(exc)
                                    raise
                        return response

                    def sleep(delay):
                        clock[0] += delay

                    timer = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
                    with patch.object(readonly, 'time', timer), \
                            patch.object(readonly.ReadConnection, '_receive', delayed), \
                            patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn:
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            readonly.connect(store, **options)
                    if preserve:
                        self.assertIs(caught.exception, errors[0])
                        self.assertNotIsInstance(caught.exception, readonly.ReadTimeout)
                    else:
                        self.assertIsInstance(caught.exception, readonly.ReadTimeout)
                    self.assertEqual(spawn.call_count, 1)
                    self.assertEqual(len(waits), 1)
                    self.assertAlmostEqual(waits[0], .0001)
                    self.assertAlmostEqual(clock[0], budget)
                    self.assertTrue(opened[0].closed)
                    self.assertIsNotNone(opened[0].process.poll())


if __name__ == '__main__':
    unittest.main()
