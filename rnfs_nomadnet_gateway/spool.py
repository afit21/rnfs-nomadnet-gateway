"""The spool (specification section 6): fetched files waiting to be downloaded.

The daemon writes each to `<spool>/<target CID>/<name>`, under a temporary hidden name renamed once complete,
so a file without a leading dot is always whole. A file's modification time is when it was last fetched or
downloaded: the least recently used are removed first when the spool is over spool_max, and any not used for
spool_ttl. A file is never removed while it is being sent.
"""
import io
import os
import shutil
import threading
import time

from rnfs import Cid

STALE_PARTIAL = 2 * 24 * 3600  # a daemon's temporary output this old belongs to no fetch that's still running


class Spool:
    """The spool directory; thread-safe."""

    def __init__(self, directory: str, max_bytes: int, ttl: float, clock=time.time):
        self.directory = directory
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._reserved: dict[str, int] = {}  # CID → bytes held for a running fetch
        self._sending: dict[object, list[tuple[str, io.BufferedReader]]] = {}  # owner (link ID) → open files
        os.makedirs(directory, mode=0o700, exist_ok=True)

    def directory_for(self, cid: str) -> str:
        """Where a CID's file goes (cid is checked, so it can't name another directory)."""
        return os.path.join(self.directory, str(Cid.parse(cid)))

    def find(self, cid: str) -> str | None:
        """The complete spooled file for cid, or None."""
        try:
            with os.scandir(self.directory_for(cid)) as entries:
                for entry in entries:
                    if not entry.name.startswith(".") and entry.is_file(follow_symlinks=False):
                        return entry.path
        except (FileNotFoundError, NotADirectoryError):
            pass
        return None

    def files(self) -> list[tuple[str, str]]:
        """(CID, path) of every complete file in the spool."""
        found = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return found
        for name in names:
            try:
                path = self.find(name)
            except ValueError:
                continue  # not a CID's directory
            if path is not None:
                found.append((name, path))
        return found

    def touch(self, path: str):
        """Mark a spooled file as used now."""
        try:
            now = self.clock()
            os.utime(path, (now, now))
        except OSError:
            pass

    # Sending

    def open_for_send(self, path: str, owner) -> io.BufferedReader:
        """path opened for RNS to send (it must be exactly an io.BufferedReader), kept from removal until RNS
        closes it or release(owner) is called when the link closes. Raises OSError."""
        file = open(path, "rb")
        with self._lock:
            self._sending.setdefault(owner, []).append((path, file))
        self.touch(path)
        return file

    def release(self, owner):
        """Close and forget the files sent over a link that has closed."""
        with self._lock:
            files = self._sending.pop(owner, [])
        for _, file in files:
            try:
                file.close()
            except OSError:
                pass

    def _in_use(self) -> set[str]:
        """Paths being sent (RNS closes a file once sent); called with the lock held."""
        in_use = set()
        for owner in list(self._sending):
            files = [(path, file) for path, file in self._sending[owner] if not file.closed]
            if files:
                self._sending[owner] = files
                in_use.update(os.path.normcase(os.path.abspath(path)) for path, _ in files)
            else:
                del self._sending[owner]
        return in_use

    # Room

    def reserve(self, cid: str, size: int) -> bool:
        """Hold room for a fetch of size bytes, removing the least recently used files if needed. False if
        there isn't room (the file is larger than the spool, or what is there is being sent or fetched)."""
        with self._lock:
            if size > self.max_bytes:
                return False
            if not self._clean(need=size + sum(self._reserved.values())):
                return False
            self._reserved[cid] = size
            return True

    def unreserve(self, cid: str):
        with self._lock:
            self._reserved.pop(cid, None)

    def clean(self):
        """Remove expired files, and the least recently used while over spool_max."""
        with self._lock:
            self._clean(need=sum(self._reserved.values()))

    def _clean(self, need: int) -> bool:
        """Expire and evict so that need more bytes fit; True if they do. Called with the lock held."""
        now = self.clock()
        in_use = self._in_use()
        files = []  # (last used, size, path, directory)
        try:
            directories = list(os.scandir(self.directory))
        except OSError:
            return False
        for directory in directories:
            if not directory.is_dir(follow_symlinks=False):
                continue
            busy = directory.name in self._reserved
            try:
                entries = list(os.scandir(directory.path))
            except OSError:
                continue
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name.startswith("."):
                    # The daemon's temporary output: removed only once long abandoned (a gateway restarted
                    # mid-fetch); a running fetch's is counted against its reservation
                    if not busy and now - info.st_mtime > STALE_PARTIAL:
                        _remove(entry.path)
                    continue
                files.append((info.st_mtime, info.st_size, entry.path, directory.path))
            if not entries and not busy:
                _remove_dir(directory.path)
        files.sort()
        total = sum(size for _, size, _, _ in files)
        for last_used, size, path, directory in files:
            if os.path.normcase(os.path.abspath(path)) in in_use:
                continue
            if now - last_used > self.ttl or total + need > self.max_bytes:
                if _remove(path):
                    total -= size
                    _remove_dir(directory)
        return total + need <= self.max_bytes

    def used(self) -> int:
        """Bytes of complete files in the spool."""
        total = 0
        for root, _, names in os.walk(self.directory):
            for name in names:
                if not name.startswith("."):
                    try:
                        total += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        pass
        return total

    def remove_empty(self, cid: str):
        """Remove cid's directory if a failed fetch left it empty."""
        _remove_dir(self.directory_for(cid))


def _remove(path: str) -> bool:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _remove_dir(path: str):
    """Remove a directory if it is empty."""
    try:
        os.rmdir(path)
    except OSError:
        pass
