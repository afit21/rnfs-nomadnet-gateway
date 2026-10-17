"""Shared test helpers."""
import base64
import hashlib
import time

RAW_PREFIX = bytes([0x01, 0x55, 0x12, 0x20])
DAG_PREFIX = bytes([0x01, 0x80, 0x80, 0xC0, 0x01, 0x12, 0x20])


def cid(seed: str, dag: bool = False) -> str:
    """A valid written CID derived from seed."""
    binary = (DAG_PREFIX if dag else RAW_PREFIX) + hashlib.sha256(seed.encode()).digest()
    return "b" + base64.b32encode(binary).decode("ascii").lower().rstrip("=")


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def wait_for(condition, timeout: float = 5.0):
    """Wait until condition() is true, failing the test after timeout."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)
