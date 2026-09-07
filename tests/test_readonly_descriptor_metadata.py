"""Path creation time and descriptor change time need independent fences."""
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


class DescriptorMetadataTests(unittest.TestCase):
    def run_reader(self, *, change=None, change_at=2, windows=True):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as c:
                c.execute('CREATE TABLE sample(value)')
                c.execute('INSERT INTO sample VALUES (1)')
                c.commit()
            # A legitimate same-store write makes ChangeTime differ from
            # CreationTime on Windows, even with no concurrent processes.
            real_fstat = os.fstat
            path_ctime = path.stat().st_ctime_ns
            responses = []
            calls = [0]

            def fstat(fd):
                st = real_fstat(fd)
                values = {name: getattr(st, name) for name in
                          ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                values['st_birthtime_ns'] = path_ctime
                values['st_ctime_ns'] = path_ctime + 1000000
                calls[0] += 1
                if change and calls[0] >= change_at:
                    values[change] += 1
                return SimpleNamespace(**values)

            requests = io.StringIO('{"sql":"SELECT value FROM sample","parameters":[]}\n')
            with patch.object(worker, '_WINDOWS', windows), \
                    patch.object(worker.os, 'fstat', side_effect=fstat), \
                    patch.object(worker.sys, 'stdin', requests), \
                    patch.object(worker, 'emit', side_effect=responses.append):
                worker.main(path)
            return responses

    def test_distinct_path_and_descriptor_ctime_allows_query(self):
        responses = self.run_reader()
        self.assertEqual(responses[-1], {'rows': [(1,)], 'names': ['value']})

    def test_descriptor_changes_remain_unavailable(self):
        for change_at in (2, 3):  # Before SQL and before returning its rows.
            for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                with self.subTest(field=field, change_at=change_at):
                    responses = self.run_reader(change=field, change_at=change_at)
                    self.assertEqual(responses[-1]['kind'], 'unavailable')
                    self.assertNotIn('rows', responses[-1])

    def test_posix_ctime_mismatch_still_rejects_binding(self):
        responses = self.run_reader(windows=False)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]['kind'], 'unavailable')

    def test_descriptor_must_bind_to_selected_path(self):
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field):
                responses = self.run_reader(change=field, change_at=1)
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0]['kind'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
