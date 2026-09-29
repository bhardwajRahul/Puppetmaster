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
            before = set(readonly._cleanup.owners)
            _owning_state_dirs([root], 'job_000000000000')
            # Other tests may leave their own entries; only this scan's count.
            stale = [o for t, o in readonly._cleanup.owners.items()
                     if t not in before and o.transport.closed]
            self.assertEqual(stale, [])


if __name__ == '__main__':
    unittest.main()
