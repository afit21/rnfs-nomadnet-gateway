import os
import tempfile
import unittest

from rnfs_nomadnet_gateway.spool import STALE_PARTIAL, Spool
from tests.support import FakeClock, cid

A, B, C = cid("a"), cid("b"), cid("c")


class SpoolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = FakeClock(10 ** 6)
        self.spool = Spool(os.path.join(self.dir.name, "spool"), 10, 100, clock=self.clock)

    def put(self, cid_, name, size, used=None):
        directory = self.spool.directory_for(cid_)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        when = self.clock.now if used is None else used
        os.utime(path, (when, when))
        return path

    def test_find_ignores_partials(self):
        self.put(A, ".rnfs-get-1.partial", 1)
        self.assertIsNone(self.spool.find(A))
        path = self.put(A, "file", 1)
        self.assertEqual(self.spool.find(A), path)
        with self.assertRaises(ValueError):
            self.spool.directory_for("../etc")

    def test_lru_eviction(self):
        self.put(A, "a", 4, used=self.clock.now - 30)
        self.put(B, "b", 4, used=self.clock.now - 10)
        self.assertTrue(self.spool.reserve(C, 4))
        self.assertIsNone(self.spool.find(A))
        self.assertIsNotNone(self.spool.find(B))
        self.assertFalse(os.path.exists(self.spool.directory_for(A)))
        self.assertFalse(self.spool.reserve(cid("d"), 11))  # larger than the spool

    def test_in_use_not_removed(self):
        self.put(A, "a", 8, used=self.clock.now - 1000)
        file = self.spool.open_for_send(self.spool.find(A), owner="link1")
        self.clock.now += 1000
        self.spool.clean()  # expired, but being sent
        self.assertIsNotNone(self.spool.find(A))
        self.assertFalse(self.spool.reserve(B, 4))
        file.close()  # RNS closes a file once sent
        self.assertTrue(self.spool.reserve(B, 4))
        self.assertIsNone(self.spool.find(A))

    def test_ttl(self):
        self.put(A, "a", 1, used=self.clock.now - 101)
        self.put(B, "b", 1, used=self.clock.now - 99)
        self.spool.clean()
        self.assertIsNone(self.spool.find(A))
        self.assertIsNotNone(self.spool.find(B))

    def test_stale_partials(self):
        old = self.put(A, ".rnfs-get-1.partial", 1, used=self.clock.now - STALE_PARTIAL - 1)
        fresh = self.put(B, ".rnfs-get-2.partial", 1)
        self.spool.clean()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_reservations_add_up(self):
        self.assertTrue(self.spool.reserve(A, 6))
        self.assertFalse(self.spool.reserve(B, 6))
        self.spool.unreserve(A)
        self.assertTrue(self.spool.reserve(B, 6))


if __name__ == "__main__":
    unittest.main()
