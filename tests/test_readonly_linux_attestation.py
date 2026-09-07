"""Real SQLite opens with Linux fd attestation (fd namespace seam on macOS)."""
import io
import os
import sqlite3
import stat
import sys
import unittest
from contextlib import closing, ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from puppetmaster import readonly_worker as worker


@unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'Unix fd namespace')
class LinuxAttestationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires native Linux procfs and SQLite VFS')
    def test_native_linux_descriptor_uri_success(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source'
            with closing(sqlite3.connect(path)) as c, c:
                c.execute('CREATE TABLE sample(value)')
                c.execute("INSERT INTO sample VALUES ('A')")
            responses = []
            with patch.object(worker, 'emit', side_effect=responses.append), \
                    patch.object(worker.sys, 'stdin', io.StringIO('{"sql":"SELECT value FROM sample","parameters":[]}\n')):
                worker.main(path)
            self.assertEqual(responses[-1]['rows'], [('A',)])

    def run_open(self, scenario):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            path, other, saved = [Path(directory) / name for name in ('source', 'other', 'saved')]
            for database, value in ((path, 'A'), (other, 'FOREIGN B')):
                with closing(sqlite3.connect(database)) as c, c:
                    c.execute('CREATE TABLE sample(value)')
                    c.execute('INSERT INTO sample VALUES (?)', (value,))
            responses, connections, sources, extras = [], [], [], []
            connect, path_open = sqlite3.connect, Path.open
            def capture_source(p, *args, **kwargs):
                source = path_open(p, *args, **kwargs)
                sources.append(source)
                return source
            def second_open(*args, **kwargs):
                if scenario == 'aba':
                    path.rename(saved)
                    other.rename(path)
                try:
                    # Canonical pathname is precisely the Linux VFS behavior.
                    c = connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
                    connections.append(c)
                    if scenario == 'aba':
                        self.assertEqual(c.execute('SELECT value FROM sample').fetchall(), [('FOREIGN B',)])
                    if scenario == 'post_missing':
                        stack.enter_context(patch.object(worker.os, 'scandir', side_effect=PermissionError('procfs unavailable')))
                    if scenario in ('extra', 'extra_same'):
                        extras.append(os.open(path if scenario == 'extra_same' else other, os.O_RDONLY))
                    return c
                finally:
                    if scenario == 'aba':
                        path.rename(other)
                        saved.rename(path)
            if sys.platform == 'darwin':
                scan, stat = os.scandir, os.stat
                stack.enter_context(patch.object(worker.os, 'scandir', side_effect=lambda p: scan('/dev/fd' if p == '/proc/self/fd' else p)))
                stack.enter_context(patch.object(worker.os, 'stat', side_effect=lambda p, *a, **kw: stat('/dev/fd' if p == '/proc/self/fd' else p, *a, **kw)))
            stack.enter_context(patch.object(worker, 'sys', SimpleNamespace(platform=sys.platform, byteorder=sys.byteorder, stdin=io.StringIO('{"sql":"SELECT value FROM sample","parameters":[]}\n'))))
            checkpoint = worker.checkpointed_sidecars
            def checked(*args):
                result = checkpoint(*args)
                worker.sys.platform = 'linux'
                return result
            stack.enter_context(patch.object(worker, 'checkpointed_sidecars', side_effect=checked))
            stack.enter_context(patch.object(Path, 'open', capture_source))
            stack.enter_context(patch.object(worker.sqlite3, 'connect', side_effect=second_open))
            stack.enter_context(patch.object(worker, 'emit', side_effect=responses.append))
            if scenario == 'missing':
                stack.enter_context(patch.object(worker.os, 'scandir', side_effect=PermissionError('procfs unavailable')))
            try:
                if scenario == 'success':
                    worker.main(path)
                    self.assertEqual(responses[-1]['rows'], [('A',)])
                else:
                    with self.assertRaisesRegex(OSError, 'attestation|procfs'):
                        worker.main(path)
                    self.assertEqual(responses, [])
            finally:
                for fd in extras:
                    os.close(fd)
            self.assertTrue(sources)
            if scenario == 'missing':
                self.assertEqual(connections, [])
            self.assertTrue(all(source.closed for source in sources))
            for c in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    c.execute('SELECT 1')
            with closing(connect(path, timeout=0)) as c:
                c.execute('BEGIN EXCLUSIVE')
                c.rollback()

    def test_canonicalized_sqlite_open_is_attested(self):
        self.run_open('success')

    def test_aba_foreign_sqlite_descriptor_is_rejected_and_closed(self):
        self.run_open('aba')

    def test_extra_descriptor_is_ambiguous_and_connection_closes(self):
        self.run_open('extra')

    def test_extra_matching_descriptor_is_also_ambiguous(self):
        self.run_open('extra_same')

    def test_procfs_failure_after_connect_closes_connection(self):
        self.run_open('post_missing')

    def test_missing_procfs_fails_before_connect_and_closes_source(self):
        self.run_open('missing')


class DescriptorProofTests(unittest.TestCase):
    def test_missing_reused_and_extra_descriptors_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source'
            path.write_bytes(b'A')
            with path.open('rb') as source:
                st = os.fstat(source.fileno())
                identity = (st.st_dev, st.st_ino, stat.S_IFREG)
                baseline = {source.fileno(): identity, 1000: (1, 2, stat.S_IFIFO)}
                for after in (
                    baseline,  # No new fd, including a cached SQLite fd.
                    {source.fileno(): identity, 1000: identity},  # Reused number.
                    {source.fileno(): identity, 1001: identity},  # Missing old fd.
                    {**baseline, 1001: identity, 1002: identity},
                    {**baseline, 1001: identity, 1002: (1, 3, stat.S_IFREG)},
                    {**baseline, 1001: (1, 3, stat.S_IFREG)},
                ):
                    with self.subTest(after=after), patch.object(worker, 'linux_fd_snapshot', return_value=after):
                        with self.assertRaisesRegex(OSError, 'attestation'):
                            worker.attest_linux_database(baseline, source.fileno())
                with patch.object(worker, 'linux_fd_snapshot', return_value={**baseline, 1001: identity}):
                    worker.attest_linux_database(baseline, source.fileno())

    def test_snapshot_does_not_ignore_disappearing_descriptor(self):
        entries = unittest.mock.MagicMock()
        entries.__enter__.return_value = [SimpleNamespace(name='1234')]
        with patch.object(worker.os, 'stat', return_value=SimpleNamespace(st_dev=1, st_ino=1)), \
                patch.object(worker.os, 'scandir', return_value=entries), \
                patch.object(worker.os, 'fstat', side_effect=OSError('fd disappeared')):
            with self.assertRaisesRegex(OSError, 'fd disappeared'):
                worker.linux_fd_snapshot()


if __name__ == '__main__':
    unittest.main()
