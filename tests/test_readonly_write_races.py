"""Only proven pre-snapshot writes retry, in the same bounded helper."""
import io
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
from puppetmaster import identity, readonly, readonly_worker as worker
from puppetmaster.sqlite_store import SQLiteSwarmStore


class ReadonlyWriteRaceTests(unittest.TestCase):
    def test_writer_between_stat_and_descriptor_is_classified(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            with closing(sqlite3.connect(path)) as c, c:
                c.execute('CREATE TABLE sample(value)')
            original = worker.source_stamp
            written = []
            responses = []
            def race(path_arg=None, *, fd=None):
                if fd is not None and not written:
                    written.append(True)
                    with closing(sqlite3.connect(path)) as c, c:
                        c.execute('INSERT INTO sample VALUES(1)')
                return original(path_arg, fd=fd)
            with patch.object(worker, 'source_stamp', side_effect=race), \
                    patch.object(worker, 'emit', side_effect=responses.append):
                worker.main(path)
            self.assertEqual(len(responses), 1)
            self.assertTrue(responses[0]['same_store_write'])
            self.assertEqual(responses[0]['kind'], 'unavailable')
            responses.clear()
            with patch.object(worker, 'emit', side_effect=responses.append), \
                    patch.object(worker.sys, 'stdin', io.StringIO(
                        '{"sql":"SELECT value FROM sample","parameters":[]}\n')):
                worker.main(path)
            self.assertEqual(responses[-1]['rows'], [(1,)])

    def test_proven_writes_bind_with_one_helper_for_each_read_mode(self):
        for options, proof in (({}, 'same_store_write'), ({'reuse': True}, 'same_store_write'),
                               ({'attach_binding': True}, 'same_store_write'),
                               ({'attach_binding': True}, 'launch_topology_change'),
                               ({'launch_binding': True}, 'same_store_write')):
            with self.subTest(options=options, proof=proof), TemporaryDirectory() as tmp:
                store = SQLiteSwarmStore(tmp)
                store.ensure_schema()
                original = readonly.ReadConnection._receive
                retries = []
                # Startup, release, and the fixture write must not consume the
                # reuse mode's 100 ms budget before its synthetic error arrives.
                # Advance retry backoff deterministically; deadline/ABA behavior
                # is exercised separately below.
                clock = [0.0]
                def sleep(delay):
                    clock[0] += delay
                count = 1 if options.get('reuse') else 2
                def race(c):
                    response = original(c)
                    if not c._opened and len(retries) < count:
                        # Park this successful test session before injecting a
                        # failure at the helper's real failed-open boundary.
                        c._opened = True
                        c._control('release', True)
                        c._opened = False
                        retries.append(c.transport)
                        store.create_job('concurrent write')
                        error = dict(kind='unavailable', error='unable to open database: source changed',
                                     **{proof: True})
                        with patch.object(c.responses, 'get', return_value=json.dumps(error)):
                            return original(c)
                    return response
                with patch.object(readonly.ReadConnection, '_receive', race), \
                        patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn, \
                        patch.object(readonly, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)):
                    with readonly.connect(store, **options) as c:
                        self.assertEqual(identity.read_identity(c, 'sqlite'), store._incarnation)
                        self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0], count)
                        self.assertEqual(len(retries), count)
                        self.assertTrue(all(t is c.transport for t in retries))
                    self.assertEqual(spawn.call_count, 1)
                if getattr(store, '_readonly_transport', None):
                    store._readonly_transport.close()

    def test_write_retry_deadline_and_aba_reap_the_only_helper(self):
        for aba in (False, True):
            with self.subTest(aba=aba), TemporaryDirectory() as tmp:
                store = SQLiteSwarmStore(tmp)
                store.ensure_schema()
                original = readonly.ReadConnection._receive
                clock = [0.0]
                opened = []
                def race(c):
                    response = original(c)
                    if not c._opened:
                        opened.append(c)
                        c._opened = True
                        c._control('release', True)
                        c._opened = False
                        clock[0] += .1
                        error = dict(kind='unavailable', error='unable to open database: source changed',
                                     same_store_write=True)
                        with patch.object(c.responses, 'get', return_value=json.dumps(error)):
                            return original(c)
                    return response
                def sleep(delay):
                    clock[0] += delay
                    if aba:
                        moved = store.db_path.with_suffix('.old')
                        store.db_path.rename(moved)
                        moved.rename(store.db_path)
                with patch.object(readonly.ReadConnection, '_receive', race), \
                        patch.object(readonly, 'ReaderProcess', wraps=readonly.ReaderProcess) as spawn, \
                        patch.object(readonly, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)):
                    with self.assertRaises(identity.StoreIdentityError if aba else readonly.ReadUnavailable):
                        readonly.connect(store, attach_binding=True, timeout=.25)
                    self.assertEqual(spawn.call_count, 1)
                self.assertTrue(opened)
                self.assertTrue(all(c.closed and c.process.poll() is not None for c in opened))
                self.assertLessEqual(clock[0], .35)

    def test_identity_metadata_and_sidecar_changes_are_not_write_proofs(self):
        before = (1, 2, 100, 200, 300)
        for after in (None, (1, 3, 100, 201, 301), (1, 2, 100, 200, 301)):
            self.assertFalse(worker.same_store_write(before, after))
        self.assertTrue(worker.same_store_write(before, (1, 2, 100, 201, 301)))
        # Windows may publish size before the last writer closes its handle.
        grown = (1, 2, 101, 200, 301)
        self.assertTrue(worker.same_store_write(before, grown))
        readonly._metadata_fence(before, grown)
        with self.assertRaises(identity.StoreIdentityError):
            readonly._metadata_fence(before, (1, 2, 100, 200, 301))


if __name__ == '__main__':
    unittest.main()
