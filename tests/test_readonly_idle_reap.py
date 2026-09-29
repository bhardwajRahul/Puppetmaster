"""A long-lived host does not keep one helper interpreter per collected store."""
import gc
import unittest
from tempfile import TemporaryDirectory

from puppetmaster import readonly
from puppetmaster.sqlite_store import SQLiteSwarmStore


class IdleHelperReapTests(unittest.TestCase):
    def test_collected_store_helper_is_reaped_by_the_next_read(self):
        with TemporaryDirectory() as first_root, TemporaryDirectory() as second_root:
            store = SQLiteSwarmStore(first_root)
            store.ensure_schema()
            with readonly.connect(store, reuse=True) as connection:
                self.assertEqual(connection.execute('SELECT 42').fetchone()[0], 42)
            transport = store._readonly_transport
            process = transport.process
            del connection, store
            gc.collect()
            self.assertFalse(transport.closed)
            other = SQLiteSwarmStore(second_root)
            other.ensure_schema()
            with readonly.connect(other, reuse=True) as connection:
                self.assertEqual(connection.execute('SELECT 7').fetchone()[0], 7)
            self.assertTrue(transport.closed)
            self.assertIsNotNone(process.poll())
            other._readonly_transport.close()


if __name__ == '__main__':
    unittest.main()
