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
            # A completed rename round trip preserves the selected file. Native
            # ChangeTime need not advance for every operation; it is a fence,
            # not an operation counter.
            with path.open('rb') as source:
                self.assertEqual(after, worker.source_stamp(fd=source.fileno()))

    def test_equal_change_time_preserves_identity_and_descriptor_fences(self):
        for race in ('round_trip', 'replacement', 'replacement_aba'):
            with self.subTest(race=race), TemporaryDirectory() as directory:
                path = Path(directory) / 'source.sqlite3'
                with closing(sqlite3.connect(path)) as c, c:
                    c.execute('CREATE TABLE sample(value)')
                    c.execute('INSERT INTO sample VALUES(1)')
                replacement = path.with_suffix('.replacement')
                replacement.write_bytes(path.read_bytes())
                original_stamp, original_stamps = worker.source_stamp, worker.stamps
                before = original_stamp(path)
                def fixed_time(path=None, *, fd=None):
                    stamp = original_stamp(path, fd=fd)
                    # Make all timestamps and sizes equal: only identity can
                    # distinguish the replacement from the selected file.
                    return stamp[:2] + before[2:]
                first = [True]
                old = path.with_suffix('.old')
                def swap(selected):
                    result = original_stamps(selected)
                    if first[0]:
                        first[0] = False
                        path.rename(old)
                        if race in ('round_trip', 'replacement_aba'):
                            old.rename(path)
                        else:
                            replacement.rename(path)
                    return result
                def descriptor(path_arg=None, *, fd=None):
                    result = fixed_time(path_arg, fd=fd)
                    if fd is not None and race == 'replacement_aba':
                        # Model an acquired B descriptor after the pathname has
                        # returned to A, without requiring Windows delete-sharing.
                        return original_stamp(replacement)[:2] + before[2:]
                    return result
                responses = []
                with patch.object(worker, 'source_stamp', side_effect=descriptor), \
                        patch.object(worker, 'stamps', side_effect=swap), \
                        patch.object(worker, 'emit', side_effect=responses.append), \
                        patch.object(worker.sys, 'stdin', io.StringIO(
                            '{"sql":"SELECT value FROM sample","parameters":[]}\n')):
                    worker.main(path)
                if race == 'round_trip':
                    self.assertEqual(responses[-1]['rows'], [(1,)])
                else:
                    self.assertEqual(responses[-1]['kind'], 'unavailable')
                    self.assertNotIn('rows', responses[-1])

    def test_descriptor_changes_remain_unavailable(self):
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
        if sys.platform != 'darwin':
            fields += ('st_ctime_ns',)
        for change_at in (2, 3):  # Before SQL and before returning its rows.
            for field in fields:
                with self.subTest(field=field, change_at=change_at):
                    responses = self.run_reader(change=field, change_at=change_at)
                    self.assertEqual(responses[-1]['kind'], 'unavailable')
                    self.assertNotIn('rows', responses[-1])

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin r+b lock-open updates ctime')
    def test_darwin_exclusive_open_ctime_only_still_reads(self):
        """APFS can bump ctime when the helper opens the db r+b for LOCK_EX.

        Observed on ~/Library/Application Support/.../state.sqlite3; tmp files
        often do not. A ctime-only drift is not a replaced file.
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.sqlite3'
            with closing(sqlite3.connect(path)) as c:
                c.execute('CREATE TABLE sample(value)')
                c.execute('INSERT INTO sample VALUES (1)')
                c.commit()
            original = worker.stamps
            responses = []
            calls = [0]

            def stamps_with_ctime_bump(selected):
                result = original(selected)
                calls[0] += 1
                if calls[0] > 1 and result[0] is not None:
                    main = result[0]
                    result = [(main[0], main[1], main[2], main[3], main[4] + 1)] + result[1:]
                return result

            with patch.object(worker, 'stamps', side_effect=stamps_with_ctime_bump), \
                    patch.object(worker.sys, 'stdin', io.StringIO(
                        '{"sql":"SELECT value FROM sample","parameters":[]}\n')), \
                    patch.object(worker, 'emit', side_effect=responses.append):
                worker.main(path)
            self.assertEqual(responses[-1]['rows'], [(1,)])
            self.assertGreater(calls[0], 1)

    def test_ctime_mismatch_still_rejects_binding(self):
        responses = self.run_reader(change='st_ctime_ns', change_at=1)
        if sys.platform == 'darwin':
            # Darwin r+b lock-open updates ctime on some volumes (including the
            # host state.sqlite3). Inode/size/mtime still fence replacement.
            self.assertEqual(responses[-1]['rows'], [(1,)])
            self.assertNotEqual(responses[-1].get('kind'), 'unavailable')
            return
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]['kind'], 'unavailable')

    def test_descriptor_must_bind_to_selected_path(self):
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
        if sys.platform != 'darwin':
            fields += ('st_ctime_ns',)
        for field in fields:
            with self.subTest(field=field):
                responses = self.run_reader(change=field, change_at=1)
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0]['kind'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
