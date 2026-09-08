"""Exercise SQLite's actual second open while the selected pathname is replaced."""
import ctypes
import io
import os
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
from puppetmaster import readonly_worker as worker


class SQLiteOpenIdentityTests(unittest.TestCase):
    def race(self, windows_seam=False, directory_aba=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            if directory_aba:
                selected, replacement, saved = root / 'selected', root / 'other', root / 'saved'
                selected.mkdir()
                replacement.mkdir()
                path, other = selected / 'source.sqlite3', replacement / 'source.sqlite3'
            else:
                path = root / 'source.sqlite3'
                other, saved = path.with_suffix('.other'), path.with_suffix('.saved')
                selected, replacement = path, other
            for database, value in ((path, 'A'), (other, 'FOREIGN B')):
                with closing(sqlite3.connect(database)) as c, c:
                    c.execute('CREATE TABLE sample(value)')
                    c.execute('INSERT INTO sample VALUES(?)', (value,))
            before = worker.stamp(path.stat())
            connect, rename = sqlite3.connect, Path.rename
            responses, bound, blocked = [], [], []
            handles = {}

            def guarded_rename(source, destination):
                if any(not share & 4 for share in handles.values()):
                    blocked.append(True)
                    raise PermissionError('delete sharing denied')
                return rename(source, destination)

            def second_open(*args, **kwargs):
                try:
                    selected.rename(saved)
                except PermissionError:
                    return connect(*args, **kwargs)
                replacement.rename(selected)
                try:
                    c = connect(*args, **kwargs)
                    # Exercise the production URI with B at the original name.
                    # Force the actual SQLite read before restoring A.
                    bound.extend(c.execute('SELECT value FROM sample').fetchall())
                    return c
                finally:
                    selected.rename(replacement)
                    saved.rename(selected)

            def create(name, access, share, security, disposition, flags, template):
                self.assertEqual(access, 0x80000000)  # GENERIC_READ
                self.assertEqual(share, 3)  # Retain concurrent read/write sharing.
                self.assertEqual(disposition, 3)  # OPEN_EXISTING
                fd = os.open(name, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
                handles[fd] = share
                return fd

            def lock(*args):
                return True

            def stamp(path=None, *, fd=None):
                value = worker.stamp(os.stat(path) if fd is None else os.fstat(fd))
                # Model Windows equal ChangeTime, keeping real file identities.
                return value[:2] + before[2:] if windows_seam else value

            with patch.object(worker, 'emit', side_effect=responses.append), \
                    patch.object(worker.sys, 'stdin', io.StringIO(
                        '{"sql":"SELECT value FROM sample","parameters":[]}\n')), \
                    patch.object(worker.sqlite3, 'connect', side_effect=second_open):
                if windows_seam:
                    kernel = SimpleNamespace(CreateFileW=create, LockFileEx=lock)
                    with patch.object(worker, 'os', SimpleNamespace(
                            name='nt', O_RDONLY=os.O_RDONLY, O_BINARY=0,
                            fdopen=os.fdopen, close=os.close)), \
                            patch.object(worker, 'source_stamp', side_effect=stamp), \
                            patch.object(Path, 'rename', guarded_rename), \
                            patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                            patch.dict(sys.modules, msvcrt=SimpleNamespace(
                                open_osfhandle=lambda handle, flags: handle,
                                get_osfhandle=lambda fd: fd)):
                        worker.main(path)
                else:
                    try:
                        worker.main(path)
                    except OSError as exc:
                        if not sys.platform.startswith('linux') or 'attestation' not in str(exc):
                            raise
                        responses.append(dict(kind='unavailable', error=str(exc)))
            self.assertFalse(any(('FOREIGN B',) in r.get('rows', []) for r in responses), responses)
            return responses, bound, blocked, handles

    @unittest.skipIf(os.name == 'nt', 'POSIX ctime fence')
    def test_posix_second_open_aba_rejects_foreign_rows(self):
        responses, bound, _, _ = self.race()
        self.assert_safe_binding(responses, bound)

    def assert_safe_binding(self, responses, bound):
        # Unix VFS builds differ in whether they resolve the descriptor URI.
        self.assertIn(bound, ([('A',)], [('FOREIGN B',)]))
        self.assertTrue(responses)
        if bound == [('FOREIGN B',)]:
            self.assertEqual(responses[-1]['kind'], 'unavailable')
            self.assertFalse(any('rows' in response for response in responses), responses)
        elif responses[-1].get('kind') == 'unavailable':
            self.assertFalse(any('rows' in response for response in responses), responses)
        else:
            self.assertEqual(responses[-1]['rows'], [('A',)])

    @unittest.skipIf(os.name == 'nt', 'POSIX directory replacement')
    def test_posix_directory_aba_rejects_foreign_rows(self):
        responses, bound, _, _ = self.race(directory_aba=True)
        self.assert_safe_binding(responses, bound)

    def test_windows_second_open_aba_with_equal_times_is_blocked(self):
        responses, bound, blocked, handles = self.race(windows_seam=True)
        self.assertTrue(blocked)
        self.assertEqual(bound, [])
        self.assertEqual(responses[-1]['rows'], [('A',)])
        for fd in handles:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_windows_wal_attach_keeps_delete_guard_without_exclusive_lock(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as c, c:
                c.execute('CREATE TABLE sample(value)')
                c.execute("INSERT INTO sample VALUES ('A')")
            before = worker.stamp(path.stat())
            responses, lock_calls = [], []
            real_connect = sqlite3.connect

            def create(name, access, share, security, disposition, flags, template):
                self.assertEqual(share, 3)
                return os.open(name, os.O_RDONLY | getattr(os, 'O_BINARY', 0))

            def lock(*args):
                lock_calls.append(args)
                return True

            def stamp(path=None, *, fd=None):
                value = worker.stamp(os.stat(path) if fd is None else os.fstat(fd))
                return value[:2] + before[2:]

            kernel = SimpleNamespace(CreateFileW=create, LockFileEx=lock)
            with patch.object(worker, 'emit', side_effect=responses.append), \
                    patch.object(worker.sys, 'stdin', io.StringIO(
                        '{"sql":"SELECT value FROM sample","parameters":[]}\n')), \
                    patch.object(worker.sqlite3, 'connect', wraps=real_connect) as connect, \
                    patch.object(worker, 'os', SimpleNamespace(
                        name='nt', O_RDONLY=os.O_RDONLY, O_BINARY=0,
                        fdopen=os.fdopen, close=os.close)), \
                    patch.object(worker, 'source_stamp', side_effect=stamp), \
                    patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                    patch.dict(sys.modules, msvcrt=SimpleNamespace(
                        open_osfhandle=lambda handle, flags: handle,
                        get_osfhandle=lambda fd: fd)):
                worker.main(path, wal_snapshot=True)

            self.assertEqual(lock_calls, [])
            self.assertEqual(connect.call_args.args[0],
                             path.resolve().as_uri() + '?mode=ro')
            self.assertEqual(responses[-1]['rows'], [('A',)])

    @unittest.skipUnless(os.name == 'nt', 'requires native Windows delete sharing')
    def test_native_windows_second_open_aba_is_blocked(self):
        responses, bound, _, _ = self.race()
        self.assertEqual(bound, [])
        self.assertEqual(responses[-1]['rows'], [('A',)])

    def test_windows_guard_open_failure_does_not_fall_back(self):
        from ctypes import wintypes
        def create(*args):
            return wintypes.HANDLE(-1).value
        kernel = SimpleNamespace(CreateFileW=create)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                patch.object(ctypes, 'get_last_error', return_value=32, create=True), \
                patch.object(ctypes, 'WinError', return_value=PermissionError('sharing violation'), create=True), \
                patch.dict(sys.modules, msvcrt=SimpleNamespace()), \
                patch.object(Path, 'open', side_effect=AssertionError('unsafe fallback')):
            with self.assertRaisesRegex(PermissionError, 'sharing violation'):
                worker.open_windows_source('source.sqlite3')

    def test_windows_initial_stamp_access_denial_is_retryable_contention(self):
        denied = PermissionError('Access is denied')
        denied.winerror = 5
        native_path = type(Path())
        with patch.object(worker, 'stamps', side_effect=denied), \
                patch.object(worker, 'Path', native_path), \
                patch.object(worker.os, 'name', 'nt'):
            with self.assertRaises(PermissionError) as caught:
                worker.main('source.sqlite3')
        self.assertTrue(caught.exception.source_open_contention)

    def test_windows_guard_sharing_denial_closes_session_and_reuses_helper(self):
        denied = PermissionError('Access is denied')
        denied.winerror = 5
        denied.source_open_contention = True
        responses = []
        with patch.object(worker, 'main', side_effect=(denied, True)) as main, \
                patch.object(worker, 'emit', side_effect=responses.append), \
                patch.object(worker.os, 'name', 'nt'), \
                patch.object(worker.sys, 'stdin', io.StringIO(
                    '{"open":"source.sqlite3"}\n')):
            worker.serve('source.sqlite3')
        self.assertTrue(responses[0]['source_open_contention'])
        self.assertTrue(responses[0]['session_closed'])
        self.assertIn('Access is denied', responses[0]['error'])
        self.assertEqual(responses[1], {'rows': [], 'names': []})
        self.assertEqual(main.call_count, 2)

    def test_only_windows_guard_open_marks_sharing_denial_as_contention(self):
        denied = PermissionError('Access is denied')
        denied.winerror = 5
        native_path = type(Path())
        with patch.object(worker, 'Path', native_path), \
                patch.object(worker, 'stamps', return_value=[
                (1, 2, 3, 4, 5), None, None, None]), \
                patch.object(worker, 'open_windows_source', side_effect=denied), \
                patch.object(worker.os, 'name', 'nt'):
            with self.assertRaises(PermissionError) as caught:
                worker.main('source.sqlite3')
        self.assertTrue(caught.exception.source_open_contention)

    def test_windows_snapshot_sharing_denial_is_session_closed_and_retried(self):
        before = [(1, 2, 3, 4, 5), None, None, None]
        responses = []

        class Source:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def fileno(self):
                return 11

            def read(self, size):
                return b'\x00' * size

        calls = {'count': 0}
        native_path = type(Path())
        run = worker.main

        def stamps(_path):
            calls['count'] += 1
            if calls['count'] == 1:
                return before
            denied = PermissionError('Access is denied')
            denied.winerror = 5
            raise denied

        def main(path, *, wal_snapshot=False):
            if calls['count']:
                return True
            return run(path, wal_snapshot=wal_snapshot)

        with patch.object(worker, 'stamps', side_effect=stamps), \
                patch.object(worker, 'main', side_effect=main) as main_mock, \
                patch.object(worker, 'open_windows_source', return_value=Source()), \
                patch.object(worker, 'source_stamp', return_value=before[0]), \
                patch.object(worker, 'emit', side_effect=responses.append), \
                patch.object(worker, 'Path', native_path), \
                patch.object(worker.os, 'name', 'nt'), \
                patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(
                    LockFileEx=lambda *args: True), create=True), \
                patch.dict(sys.modules, msvcrt=SimpleNamespace(
                    open_osfhandle=lambda handle, flags: handle,
                    get_osfhandle=lambda fd: fd)), \
                patch.object(worker.sys, 'stdin', io.StringIO(
                    '{"open":"source.sqlite3"}\n')):
            worker.serve('source.sqlite3')
        self.assertEqual(main_mock.call_count, 2)
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0]['source_open_contention'])
        self.assertTrue(responses[0]['session_closed'])
        self.assertIn('Access is denied', responses[0]['error'])
        self.assertEqual(responses[1], {'rows': [], 'names': []})

    def test_windows_snapshot_identity_failure_is_terminal(self):
        failure = OSError('SQLite fd attestation: existing descriptors changed')
        responses = []

        class Source:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def fileno(self):
                return 11

            def read(self, size):
                return b'\x00' * size

        calls = {'count': 0}
        native_path = type(Path())

        def stamps(_path):
            calls['count'] += 1
            if calls['count'] == 1:
                return [(1, 2, 3, 4, 5), None, None, None]
            raise failure

        with patch.object(worker, 'stamps', side_effect=stamps), \
                patch.object(worker, 'main', wraps=worker.main) as main, \
                patch.object(worker, 'open_windows_source', return_value=Source()), \
                patch.object(worker, 'source_stamp', return_value=(1, 2, 3, 4, 5)), \
                patch.object(worker, 'emit', side_effect=responses.append), \
                patch.object(worker, 'Path', native_path), \
                patch.object(worker.os, 'name', 'nt'), \
                patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(
                    LockFileEx=lambda *args: True), create=True), \
                patch.dict(sys.modules, msvcrt=SimpleNamespace(
                    open_osfhandle=lambda handle, flags: handle,
                    get_osfhandle=lambda fd: fd)), \
                patch.object(worker.sys, 'stdin', io.StringIO(
                    '{"open":"source.sqlite3"}\n')):
            worker.serve('source.sqlite3')
        self.assertEqual(main.call_count, 1)
        self.assertEqual(len(responses), 1)
        self.assertNotIn('session_closed', responses[0])
        self.assertIn('existing descriptors changed', responses[0]['error'])

    def test_windows_guard_closes_handle_if_crt_transfer_fails(self):
        closed = []
        def create(*args):
            return 123
        def close(handle):
            closed.append(handle)
            return True
        def transfer(*args):
            raise OSError('CRT transfer failed')
        with patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(
                CreateFileW=create, CloseHandle=close), create=True), \
                patch.object(worker.os, 'O_BINARY', 0, create=True), \
                patch.dict(sys.modules, msvcrt=SimpleNamespace(open_osfhandle=transfer)):
            with self.assertRaisesRegex(OSError, 'CRT transfer failed'):
                worker.open_windows_source('source.sqlite3')
        self.assertEqual(closed, [123])


@unittest.skipIf(os.name == 'nt', 'POSIX descriptor namespace')
class DescriptorOpenTests(unittest.TestCase):
    def test_namespace_selection_validates_opened_identity(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source'
            path.write_bytes(b'A')
            with path.open('rb') as source:
                for namespace in ('/proc/self/fd', '/dev/fd'):
                    opened = []
                    def probe(candidate, flags):
                        self.assertEqual(flags, os.O_RDONLY)
                        if str(candidate.parent) != namespace:
                            raise FileNotFoundError(candidate)
                        fd = os.dup(source.fileno())
                        opened.append(fd)
                        return fd
                    with self.subTest(namespace=namespace), patch.object(worker.os, 'open', side_effect=probe):
                        self.assertEqual(worker.descriptor_uri(source.fileno()),
                                         f'file://{namespace}/{source.fileno()}?mode=ro&immutable=1')
                    for fd in opened:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    os.fstat(source.fileno())

    def test_namespace_missing_wrong_identity_and_probe_failure_close(self):
        with TemporaryDirectory() as directory:
            path, other = Path(directory) / 'source', Path(directory) / 'other'
            path.write_bytes(b'A')
            other.write_bytes(b'B')
            with path.open('rb') as source, other.open('rb') as foreign:
                with patch.object(worker.os, 'open', side_effect=PermissionError('namespace denied')):
                    with self.assertRaisesRegex(OSError, 'no usable'):
                        worker.descriptor_uri(source.fileno())
                opened = []
                def probe(*args):
                    fd = os.dup(foreign.fileno())
                    opened.append(fd)
                    return fd
                with patch.object(worker.os, 'open', side_effect=probe):
                    with self.assertRaisesRegex(OSError, 'no usable'):
                        worker.descriptor_uri(source.fileno())
                real_fstat = os.fstat
                def broken_stamp(fd):
                    if fd == source.fileno():
                        return real_fstat(fd)
                    raise OSError('probe stat failed')
                with patch.object(worker.os, 'open', side_effect=probe), \
                        patch.object(worker.os, 'fstat', side_effect=broken_stamp):
                    with self.assertRaisesRegex(OSError, 'probe stat failed'):
                        worker.descriptor_uri(source.fileno())
                for fd in opened:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_failed_namespace_connect_and_canonicalization_never_fall_back(self):
        failures = ['namespace', 'connect', 'query']
        if not sys.platform.startswith('linux'):
            failures.append('canonicalization')
        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                path = Path(directory) / 'source.sqlite3'
                with closing(sqlite3.connect(path)) as c:
                    c.execute('CREATE TABLE sample(value)')
                sources, connections, responses = [], [], []
                original_open, original_connect = Path.open, sqlite3.connect
                def capture_source(p, *args, **kwargs):
                    source = original_open(p, *args, **kwargs)
                    sources.append(source)
                    return source
                def connect(database, **kwargs):
                    self.assertIn('/fd/', database)
                    self.assertTrue(database.endswith('?mode=ro&immutable=1'))
                    self.assertFalse(sources[0].closed)
                    if failure == 'connect':
                        raise sqlite3.OperationalError('descriptor open failed')
                    target = path.as_uri() + '?mode=ro&immutable=1' if failure == 'canonicalization' else database
                    c = original_connect(target, **kwargs)
                    if failure == 'query':
                        c.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
                    connections.append(c)
                    return c
                real_uri = worker.descriptor_uri
                def uri(fd):
                    if failure == 'namespace':
                        raise OSError('namespace unavailable')
                    return real_uri(fd)
                with patch.object(Path, 'open', capture_source), \
                        patch.object(worker, 'descriptor_uri', side_effect=uri), \
                        patch.object(worker.sqlite3, 'connect', side_effect=connect) as opens, \
                        patch.object(worker, 'emit', side_effect=responses.append):
                    with self.assertRaises((OSError, sqlite3.Error)):
                        worker.main(path)
                self.assertEqual(opens.call_count, 0 if failure == 'namespace' else 1)
                self.assertEqual(responses, [])
                self.assertTrue(all(source.closed for source in sources))
                for c in connections:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        c.execute('SELECT 1')
                with closing(original_connect(path, timeout=0)) as c:
                    c.execute('BEGIN EXCLUSIVE')
                    c.rollback()

    def test_immutable_descriptor_open_ignores_live_wal(self):
        # This intentionally bypasses main's checkpoint gate to prove the URI
        # itself ignores sidecars. Production must reject this live source.
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE sample(value)')
                writer.execute("INSERT INTO sample VALUES('checkpointed')")
                writer.commit()
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                writer.execute("UPDATE sample SET value='only in WAL'")
                writer.commit()
                files = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
                before = [(p.read_bytes(), p.stat().st_mode) for p in files]
                with path.open('rb') as source:
                    with closing(sqlite3.connect(worker.descriptor_uri(source.fileno()), uri=True)) as reader:
                        self.assertEqual(reader.execute('SELECT value FROM sample').fetchall(), [('checkpointed',)])
                        with self.assertRaises(sqlite3.OperationalError):
                            reader.execute("UPDATE sample SET value='write'")
                self.assertEqual([(p.read_bytes(), p.stat().st_mode) for p in files], before)

    @unittest.skipIf(sys.platform.startswith('linux'), 'Linux attests the opened fd instead')
    def test_actual_sqlite_symlink_resolution_is_rejected(self):
        with TemporaryDirectory() as directory:
            path, alias = Path(directory) / 'source.sqlite3', Path(directory) / 'fd'
            with closing(sqlite3.connect(path)) as c:
                c.execute('CREATE TABLE sample(value)')
            alias.symlink_to(path)
            responses = []
            # Exercise the real Unix VFS symlink resolver, as used for Linux
            # procfs descriptors, rather than mocking PRAGMA database_list.
            with patch.object(worker, 'descriptor_uri', return_value=alias.as_uri() + '?mode=ro&immutable=1'), \
                    patch.object(worker, 'emit', side_effect=responses.append):
                with self.assertRaisesRegex(OSError, 'resolved the descriptor'):
                    worker.main(path)
            self.assertEqual(responses, [])


if __name__ == '__main__':
    unittest.main()
