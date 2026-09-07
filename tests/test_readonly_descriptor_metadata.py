"""Path creation time and descriptor change time need independent fences."""
import io
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster import readonly_worker as worker


class DescriptorMetadataTests(unittest.TestCase):
    def run_reader(self, *, change=None, change_at=2):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as c:
                c.execute('CREATE TABLE sample(value)')
                c.execute('INSERT INTO sample VALUES (1)')
                c.commit()
            # A legitimate same-store write makes ChangeTime differ from
            # CreationTime on Windows, even with no concurrent processes.
            original = worker.source_stamp
            responses = []
            calls = [0]
            def source_stamp(path=None, *, fd=None):
                result = list(original(path, fd=fd))
                if fd is not None:
                    calls[0] += 1
                    if change and calls[0] >= change_at:
                        result[('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns').index(change)] += 1
                return tuple(result)

            requests = io.StringIO('{"sql":"SELECT value FROM sample","parameters":[]}\n')
            with patch.object(worker, 'source_stamp', side_effect=source_stamp), \
                    patch.object(worker.sys, 'stdin', requests), \
                    patch.object(worker, 'emit', side_effect=responses.append):
                worker.main(path)
            return responses

    def test_path_and_descriptor_stamps_allow_query(self):
        responses = self.run_reader()
        self.assertEqual(responses[-1], {'rows': [(1,)], 'names': ['value']})

    def test_native_stamps_share_time_domain_after_write_and_rename(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source'
            path.write_bytes(b'first')
            path.write_bytes(b'updated')
            with path.open('rb') as source:
                self.assertEqual(worker.source_stamp(path), worker.source_stamp(fd=source.fileno()))
            before = worker.source_stamp(path)
            moved = path.with_suffix('.old')
            path.rename(moved)
            moved.rename(path)
            after = worker.source_stamp(path)
            self.assertEqual(before[:4], after[:4])
            self.assertNotEqual(before[4], after[4])

    def test_descriptor_changes_remain_unavailable(self):
        for change_at in (2, 3):  # Before SQL and before returning its rows.
            for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                with self.subTest(field=field, change_at=change_at):
                    responses = self.run_reader(change=field, change_at=change_at)
                    self.assertEqual(responses[-1]['kind'], 'unavailable')
                    self.assertNotIn('rows', responses[-1])

    def test_ctime_mismatch_still_rejects_binding(self):
        responses = self.run_reader(change='st_ctime_ns', change_at=1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]['kind'], 'unavailable')

    def test_descriptor_must_bind_to_selected_path(self):
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
            with self.subTest(field=field):
                responses = self.run_reader(change=field, change_at=1)
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0]['kind'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
