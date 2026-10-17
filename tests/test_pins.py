import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import rnfs

from rnfs_nomadnet_gateway.pins import PinLedger
from tests.support import FakeClock, cid

A, B, C = cid("a"), cid("b"), cid("c")


class FakeApi:
    """The pin methods of rnfs.Client."""

    def __init__(self):
        self.sources: dict[str, set] = {}
        self.calls = []

    def pins(self, c):
        return [rnfs.Pin.from_dict({"cid": c, "sources": sorted(self.sources[c])})] if self.sources.get(c) else []

    def pin(self, target):
        self.calls.append(("pin", target))
        self.sources.setdefault(target, set()).add("user")

    def unpin(self, c):
        self.calls.append(("unpin", c))
        if "user" not in self.sources.get(c, ()):
            raise rnfs.NotFound("not pinned by the user")
        self.sources[c].discard("user")


class PinLedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "pins.json")
        self.clock = FakeClock()
        self.api = FakeApi()

    def ledger(self, quota=10):
        return PinLedger(self.path, quota, self.api, clock=self.clock)

    def test_quota_releases_least_recently_used(self):
        ledger = self.ledger()
        self.assertTrue(ledger.pin(A, A, 4))
        self.clock.now += 1
        self.assertTrue(ledger.pin(B, B, 4))
        self.clock.now += 1
        ledger.touch(A)  # A used more recently than B
        self.clock.now += 1
        self.assertTrue(ledger.pin(C, C, 4))
        self.assertEqual(set(ledger.pins), {A, C})
        self.assertNotIn("user", self.api.sources[B])
        self.assertEqual(ledger.used, 8)

    def test_larger_than_quota_not_pinned(self):
        self.assertFalse(self.ledger().pin(A, A, 11))
        self.assertEqual(self.api.calls, [])

    def test_operators_pins_never_released(self):
        self.api.sources[A] = {"user"}  # pinned by the operator
        ledger = self.ledger()
        self.assertFalse(ledger.pin(A, A, 4))
        self.api.sources[B] = {"folder"}  # only the shared folder's: the gateway adds "user", and may release it
        self.assertTrue(ledger.pin(B, B, 8))
        self.assertTrue(ledger.pin(C, C, 8))
        self.assertEqual(self.api.sources[A], {"user"})
        self.assertEqual(self.api.sources[B], {"folder"})

    def test_persisted(self):
        ledger = self.ledger()
        ledger.pin(A, A, 4)
        with open(self.path) as f:
            self.assertIn(A, json.load(f)["pins"])
        self.assertEqual(set(self.ledger().pins), {A})

    def test_pins_added_concurrently(self):
        """A slow pin holds up no other pin, and a pin still being added is never released for room."""
        started, gate = threading.Event(), threading.Event()
        add = self.api.pin

        def slow_add(target):
            if target == A:
                started.set()
                gate.wait(5)
            add(target)
        self.api.pin = slow_add
        ledger = self.ledger()
        first = threading.Thread(target=ledger.pin, args=(A, A, 6))
        first.start()
        self.assertTrue(started.wait(5))
        self.clock.now += 1
        self.assertFalse(ledger.pin(B, B, 6))  # no room: A can't be released while it is being added
        self.assertTrue(ledger.pin(C, C, 4))  # fits beside A, though A's pin hasn't returned
        self.assertFalse(ledger.pin(A, A, 6))  # being pinned already
        gate.set()
        first.join(5)
        self.assertEqual(set(ledger.pins), {A, C})
        self.assertIn("user", self.api.sources[A])

    def test_record_count_bounded(self):
        """Empty files never fill the quota, so records are also kept to MAX_PINS."""
        with mock.patch("rnfs_nomadnet_gateway.pins.MAX_PINS", 2):
            ledger = self.ledger()
            for c in (A, B, C):
                self.clock.now += 1
                self.assertTrue(ledger.pin(c, c, 0))
        self.assertEqual(set(ledger.pins), {B, C})

    def test_failed_pin_not_recorded(self):
        def failing_add(target):
            raise rnfs.NotFound("no providers found")
        self.api.pin = failing_add
        ledger = self.ledger()
        with self.assertRaises(rnfs.NotFound):
            ledger.pin(A, A, 4)
        self.assertEqual(ledger.pins, {})
        self.assertEqual(ledger._adding, set())

    def test_released_by_hand(self):
        ledger = self.ledger()
        ledger.pin(A, A, 8)
        self.api.sources[A].discard("user")  # rnfs pin rm by hand
        self.assertTrue(ledger.pin(B, B, 8))  # releasing A finds no pin, which is fine
        self.assertEqual(set(ledger.pins), {B})


if __name__ == "__main__":
    unittest.main()
