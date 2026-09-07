"""Permission enforcement must not invalidate an unchanged readonly source."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster import fs_permissions, readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class PrivatePermissionsTests(unittest.TestCase):
    @unittest.skipUnless(fs_permissions.supports_posix_modes(), 'POSIX permissions')
    def test_reenforcing_private_permissions_preserves_active_reader(self):
        with TemporaryDirectory() as root:
            store = SQLiteSwarmStore(root)
            store.create_job('permissions')
            before = readonly._source_stamp(store)
            with readonly.connect(store) as connection:
                fs_permissions.chmod_private_dir(store.root)
                fs_permissions.chmod_private_file(store.db_path)
                self.assertEqual(connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(readonly._source_stamp(store), before)

    @unittest.skipUnless(fs_permissions.supports_posix_modes(), 'POSIX permissions')
    def test_incorrect_modes_are_still_restricted(self):
        with TemporaryDirectory() as root:
            directory = Path(root)
            path = directory / 'file'
            path.touch()
            directory.chmod(0o755)
            path.chmod(0o644)
            fs_permissions.chmod_private_dir(directory)
            fs_permissions.chmod_private_file(path)
            self.assertEqual(directory.stat().st_mode & 0o7777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o7777, 0o600)

    def test_windows_does_not_stat_or_chmod(self):
        with patch.object(fs_permissions, 'supports_posix_modes', return_value=False), \
                patch.object(os, 'stat', side_effect=AssertionError('stat on Windows')), \
                patch.object(os, 'chmod', side_effect=AssertionError('chmod on Windows')):
            fs_permissions.chmod_private_dir('unused')
            fs_permissions.chmod_private_file('unused')
