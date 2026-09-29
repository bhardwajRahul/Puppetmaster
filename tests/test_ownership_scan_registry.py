"""An ownership scan leaves no readonly cleanup-registry entry behind."""
import unittest
from tempfile import TemporaryDirectory

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.state import _owning_state_dirs


class OwnershipScanRegistryTests(unittest.TestCase):
    def test_scan_does_not_grow_the_owner_registry(self):
        with TemporaryDirectory() as root:
            SQLiteSwarmStore(root).ensure_schema()
            _owning_state_dirs([root], 'job_000000000000')
            stale = [o for o in readonly._cleanup.owners.values() if o.transport.closed]
            self.assertEqual(stale, [])


if __name__ == '__main__':
    unittest.main()
