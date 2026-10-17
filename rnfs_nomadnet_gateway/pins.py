"""The pins the gateway made itself (small files, auto_pin_max_size, and files fetched from inside a folder, each
pinned as itself under its own CID), kept within auto_pin_quota.

The gateway pins a file through the API, which gives it the source "user", like `rnfs pin add`. So that the quota
never releases the operator's own pins, a file already pinned "user" when the gateway would pin it is left alone
and not recorded; only pins the gateway added are recorded, in `<gateway dir>/pins.json`, and only those are
released (rnfs.Client.unpin, which removes the "user" source and leaves any other, e.g. "folder"). When a new pin
would go over the quota, the least recently used recorded pins are released first. A pin the operator adds by
hand to a file the gateway pinned can't be told apart from the gateway's, and is released with it.
"""
import json
import os
import threading
import time

import rnfs

PINS_FILE = "pins.json"
# Records kept at most, whatever their sizes: empty files never fill the quota, and every pin rewrites the file
MAX_PINS = 10_000
USER = "user"


class PinLedger:
    """Records and releases the gateway's pins; thread-safe."""

    def __init__(self, path: str, quota: int, api: rnfs.Rnfs, clock=time.time, log=None):
        self.path = path
        self.quota = quota
        self.api = api
        self.clock = clock
        self.log = log or (lambda message: None)
        self.pins: dict[str, dict] = {}  # CID → {"size": bytes, "used": last used (wall time)}
        self.dirty = False
        self._adding: set[str] = set()  # CIDs recorded whose pin_add hasn't returned: never released meanwhile
        self._lock = threading.Lock()  # the records
        self._pinning = threading.Lock()  # one quota decision or release at a time, so the quota holds
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log(f"ignoring unreadable {self.path}: {e}")
            return
        for cid, entry in (data.get("pins", {}) if isinstance(data, dict) else {}).items():
            try:
                rnfs.Cid.parse(cid)
            except ValueError:
                continue
            if isinstance(entry, dict) and type(entry.get("size")) is int and isinstance(entry.get("used"), (int, float)):
                self.pins[cid] = {"size": entry["size"], "used": float(entry["used"])}

    def save(self):
        """Write the records (atomically) if they changed."""
        with self._lock:
            if not self.dirty:
                return
            data = json.dumps({"pins": self.pins}, indent=1, sort_keys=True).encode()
            self.dirty = False
        temp = f"{self.path}.{os.getpid()}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(temp, self.path)
        except OSError as e:
            self.log(f"can't save {self.path}: {e}")
            with self._lock:
                self.dirty = True

    @property
    def used(self) -> int:
        with self._lock:
            return sum(entry["size"] for entry in self.pins.values())

    def holds(self, cid: str) -> bool:
        """True if cid is one of the gateway's pins, complete."""
        with self._lock:
            return cid in self.pins and cid not in self._adding

    def touch(self, cid: str):
        """Mark one of the gateway's pins as just used (looked up or downloaded)."""
        with self._lock:
            entry = self.pins.get(cid)
            if entry is not None:
                entry["used"] = self.clock()
                self.dirty = True

    def pin(self, target: str, cid: str, size: int) -> bool:
        """Pin target (whose CID is cid, size bytes) on the rnfs node within the quota, releasing the least
        recently used of the gateway's pins to make room. False if it isn't pinned by the gateway: larger than
        the quota, the operator's own pin already, being pinned already, or no room while the gateway's other
        pins are still being added. Raises rnfs.ApiError."""
        if size > self.quota:
            return False
        # The room is made and recorded under _pinning, but the pin (a fetch, which may be slow) is added
        # without it, so other pins needn't wait for it
        with self._pinning:
            with self._lock:
                if cid in self._adding:
                    return False  # being pinned already
                previous = self.pins.get(cid)
            if previous is None and any(USER in pin.sources for pin in self.api.pins(cid)):
                return False  # the operator's own pin: never recorded, so never released
            if not self._make_room(size, keep=cid):
                return False
            with self._lock:
                self.pins[cid] = {"size": size, "used": self.clock()}
                self.dirty = True
                self._adding.add(cid)
        try:
            self.api.pin(target)  # for one of ours, makes sure it is still complete
        except BaseException:
            with self._lock:
                if previous is None:
                    self.pins.pop(cid, None)
                    self.dirty = True
            raise
        finally:
            with self._lock:
                self._adding.discard(cid)
        self.save()
        return True

    def _make_room(self, size: int, keep: str) -> bool:
        """Release the least recently used of the gateway's pins until size more bytes fit; False if they can't,
        as the rest are still being added. _pinning held."""
        while True:
            with self._lock:
                others = {cid: entry for cid, entry in self.pins.items() if cid != keep}
                if sum(entry["size"] for entry in others.values()) + size <= self.quota and len(others) < MAX_PINS:
                    return True
                releasable = [cid for cid in others if cid not in self._adding]
                if not releasable:
                    return False
                oldest = min(releasable, key=lambda cid: others[cid]["used"])
            self.release(oldest)

    def release(self, cid: str):
        """Unpin one of the gateway's pins (its "user" source) and forget it. Raises rnfs.ApiError."""
        try:
            self.api.unpin(cid)
        except rnfs.NotFound:  # not pinned "user" any more (removed by hand)
            pass
        with self._lock:
            self.pins.pop(cid, None)
            self.dirty = True
        self.log(f"released the pin of {cid} (auto_pin_quota)")
