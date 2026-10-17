"""The gateway's core (specification sections 3-7): access, target states, the worker pool and the responses.

Pages never wait on the network: every API call that may fetch runs on the worker pool, and handlers only read
its results from memory, under one lock. Lookups and listings are kept by target (their results never change,
since CIDs are immutable); fetches by the target's CID, since several targets can name one file.
"""
import base64
import os
import re
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import RNS
import rnfs
from rnfs.fsutil import safe_default_name

from rnfs_nomadnet_gateway import micron, pages, webpage
from rnfs_nomadnet_gateway.config import Config
from rnfs_nomadnet_gateway.limits import Limits
from rnfs_nomadnet_gateway.micron import FILE_PATH, GET_HTML, GET_PAGE, INDEX_HTML, INDEX_PAGE
from rnfs_nomadnet_gateway.pins import PINS_FILE, PinLedger
from rnfs_nomadnet_gateway.spool import Spool
from rnfs_nomadnet_gateway.targets import Target, parse_target

ANONYMOUS = "anonymous"  # every unidentified visitor in open mode
MAX_TARGETS = 2000  # targets whose state is kept; the least recently viewed idle ones are forgotten first
MAX_PENDING = 64  # tasks queued or running on the pool; more are refused as busy
# Directory entries kept in memory across listings (rnfs lists up to 100,000 per directory, and many targets can
# name one directory); the least recently listed are dropped first, and listed again if viewed
MAX_LISTED_ENTRIES = 100_000
MAX_WAITING = 16  # requests waiting (page_wait) at once, each on its own RNS thread; more answer at once
RECENT_FETCHES = 10  # kept per visitor
MAX_VISITORS = 1000  # visitors whose recent fetches are kept
MAX_VALUE = 8192  # characters of a request variable read
LINK_SYNTAX = "`|=]"  # characters NomadNet reads as a link's syntax: a link variable can't hold them
MAX_DOWNLOAD_PATHS = 5000  # per-file download paths registered; the least recently offered are dropped first
# A download path's file name: only characters no client reads as URL or link syntax (else the CID is used)
PATH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._()+-]{0,199}")

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
MICRON_PATHS = {INDEX_HTML: INDEX_PAGE, GET_HTML: GET_PAGE}  # an HTML page → the Micron page it converts
FALSE = ("0", "no", "false", "off")


class Raw(bytes):
    """A response sent as it is (print=1, base64=1): the content itself, not a gateway page."""


def log(message: str, level: int = RNS.LOG_INFO):
    """Log through RNS with a prefix, non-printable characters escaped (messages carry DAG names)."""
    text = "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in message)
    RNS.log(f"[gateway] {text}", level)


@dataclass
class Task:
    """A lookup, listing or fetch on the worker pool."""

    status: str = QUEUED
    result: object = None
    error: str | None = None
    created: float = 0.0
    finished: float = 0.0
    event: threading.Event = field(default_factory=threading.Event)  # set once done or failed


@dataclass
class Visitor:
    key: str  # identity hash (hex) or ANONYMOUS: what limits are counted by
    identity: str | None  # identity hash (hex) if the visitor identified
    allowed: bool


class Entry:
    """What is known about one target."""

    def __init__(self, target: Target):
        self.target = target
        self.info: Task | None = None
        self.listing: Task | None = None

    def busy(self) -> bool:
        return any(task is not None and task.status in (QUEUED, RUNNING) for task in (self.info, self.listing))


def request_value(data, name: str) -> str | None:
    """A form field (field_<name>) if filled in, else a link variable (var_<name>); None if neither."""
    if not isinstance(data, dict):
        return None
    # Clients other than NomadNet may pack keys as bytes, or send a field without its prefix
    data = {key.decode("utf-8", "replace") if isinstance(key, bytes) else key: value for key, value in data.items()}
    for key in (f"field_{name}", f"var_{name}", name):
        value = data.get(key)
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if isinstance(value, str) and value.strip():
            return value[:MAX_VALUE]
    return None


def target_value(data) -> str | None:
    """The target: `target` (the form field or a link variable), or `cid`, as in `:/page/index.mu`cid=<CID>`."""
    return request_value(data, "target") or request_value(data, "cid")


def flag(data, name: str) -> bool:
    """True if a flag variable is set, e.g. `print=1`."""
    value = request_value(data, name)
    return value is not None and value.strip().lower() not in FALSE


def output_name(data) -> tuple[str | None, str]:
    """The download name asked for with `o=<file name>` if it is fit for one, and a note if it was refused.

    It must pass rnfs's default-name rule like any name chosen by someone else, and be carried on in links."""
    value = request_value(data, "o")
    if value is None:
        return None, ""
    value = value.strip()
    if (safe_default_name(value, None) is None or any(c in value for c in LINK_SYNTAX)
            or len(value.encode("utf-8")) > 255):
        shown = value if len(value) <= 64 else value[:64] + "…"
        return None, f"Can't use the name {shown}, so it saves as the default."
    return value, ""


class Gateway:
    """Answers the gateway's requests; thread-safe."""

    def __init__(self, config: Config, api, spool: Spool, clock=time.monotonic, pins: PinLedger | None = None):
        self.config = config
        self.api = api
        self.spool = spool
        self.pins = pins or PinLedger(config.path(PINS_FILE), config.auto_pin_quota, api, log=log)
        self.clock = clock
        self.allowed = config.allowed
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.fetches: dict[str, Task] = {}  # target CID → its fetch
        self.recent: OrderedDict[str, deque] = OrderedDict()  # identity hash → (target, o) fetched, newest first
        self.limits = Limits(config.max_fetches, config.max_fetches_per_visitor, config.fetches_per_hour, clock)
        self.pending = 0
        self.listed: OrderedDict[str, int] = OrderedDict()  # target key → entries in its kept listing
        self.waiting = threading.BoundedSemaphore(MAX_WAITING)
        # Per-file download paths, /file/<cid>/<name>: a link with no variables, which every client follows
        # (some send a /file/ link's variables as part of its path). The node registers each as a handler
        self.download_paths: OrderedDict[str, tuple[Target, str, str]] = OrderedDict()  # path → target, CID, name
        self.viewable: OrderedDict[str, bool] = OrderedDict()  # CID → whether its spooled file can be viewed
        self.register_path = lambda path: None
        self.deregister_path = lambda path: None
        self.address: str | None = None  # the node's address (hex), for the start page's instructions
        self.wait_clock = time.monotonic  # real time, for page_wait
        self.pool = ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="gateway-worker")

    def close(self):
        """Stop starting tasks; running API calls are abandoned with the process."""
        self.pool.shutdown(wait=False, cancel_futures=True)

    # Access (section 4)

    def visitor(self, identity_hash: bytes | None) -> Visitor:
        identity = identity_hash.hex() if identity_hash else None
        if self.config.access == "open":
            return Visitor(identity or ANONYMOUS, identity, True)
        return Visitor(identity or ANONYMOUS, identity, identity_hash is not None and identity_hash in self.allowed)

    def respond(self, path: str, data, identity_hash: bytes | None, owner=None):
        """The response to a request on path: page bytes, or [file, {"name": bytes}] for a download.

        A page asked for on its .html path, or with html=1, is sent as HTML (webpage.py)."""
        html = path in MICRON_PATHS or (self.config.html_pages and path != FILE_PATH and flag(data, "html"))
        response = self._respond(MICRON_PATHS.get(path, path), data, identity_hash, owner, html)
        if isinstance(response, Raw):
            return bytes(response)
        if html and isinstance(response, bytes):
            return webpage.to_html(response.decode("utf-8"))
        return response

    def _respond(self, path: str, data, identity_hash: bytes | None, owner, html: bool):
        visitor = self.visitor(identity_hash)
        if not visitor.allowed:
            return pages.refused(visitor.identity)
        if path == INDEX_PAGE:
            if target_value(data) is not None:  # :/page/index.mu`cid=<CID>[|o=<file name>]
                return self.get_page(visitor, data, html)
            return self.index_page(visitor)
        if path == GET_PAGE:
            return self.get_page(visitor, data, html)
        if path == FILE_PATH:
            return self.file_response(visitor, data, owner)
        return micron.page("Not found", "There's no such page here.")

    # Tasks

    def _waiting(self, task: Task) -> bool:
        """True if a queued task waits for a worker (not just to be picked up by a free one). Lock held."""
        return task.status == QUEUED and self.pending > self.config.max_workers

    def _expired(self, task: Task) -> bool:
        return task.status == FAILED and self.clock() - task.finished >= self.config.failure_ttl

    def _submit(self, function, *args) -> Task | None:
        """A new task running function(task, *args) on the pool, or None if too much is queued. Lock held."""
        if self.pending >= MAX_PENDING:
            return None
        task = Task(created=self.clock())
        self.pending += 1
        try:
            self.pool.submit(self._run, task, function, *args)
        except RuntimeError:  # shut down
            self.pending -= 1
            return None
        return task

    def _run(self, task: Task, function, *args):
        with self.lock:
            task.status = RUNNING
        try:
            result = function(*args)
        except Exception as e:  # noqa: BLE001 - every failure is shown and remembered
            error = self._describe(e)
            with self.lock:
                task.status, task.error, task.finished = FAILED, error, self.clock()
        else:
            with self.lock:
                task.status, task.result, task.finished = DONE, result, self.clock()
        finally:
            with self.lock:
                self.pending -= 1
            task.event.set()

    def _describe(self, error: Exception) -> str:
        """What a visitor is told about a failed API call (local paths left out; details go to the log)."""
        if not isinstance(error, rnfs.ApiError):
            log(f"internal error: {error!r}", RNS.LOG_ERROR)
            return "Something went wrong in the gateway."
        message = str(error)
        for path in self.private_paths():
            if len(path) > 1:
                message = message.replace(path, "…")
        prefixes = {400: "", 404: "Not found: ", 413: "Too big: ", 422: "Broken content: ",
                    451: "Not available here: "}
        if error.status in prefixes:
            return prefixes[error.status] + message
        if error.status == 503:
            return "rnfs is busy. Try again later."
        if error.status == 507:
            return "The gateway's rnfs storage is full."
        log(f"API request failed: {error}", RNS.LOG_WARNING)
        if error.status is None:
            return "The gateway can't reach rnfs right now."
        return "rnfs had an internal error."

    def private_paths(self) -> list[str]:
        """Local paths kept out of what visitors see (error messages may name them), longest first."""
        paths = (self.config.gateway_dir, self.config.rnfs_dir, "~")
        found = {os.path.expanduser(p) for p in paths} | {os.path.realpath(os.path.expanduser(p)) for p in paths}
        return sorted(found, key=len, reverse=True)

    def _entry(self, target: Target) -> Entry:
        """The target's entry, created if new and marked as just used. Lock held."""
        entry = self.entries.get(target.key)
        if entry is None:
            entry = self.entries[target.key] = Entry(target)
            if len(self.entries) > MAX_TARGETS:
                for key in list(self.entries)[:len(self.entries) - MAX_TARGETS]:
                    if not self.entries[key].busy():
                        del self.entries[key]
        self.entries.move_to_end(target.key)
        return entry

    def _ensure(self, entry: Entry, kind: str, function) -> Task | None:
        """The entry's lookup or listing task, started if there is none or its failure has expired. None if
        the pool is too busy to start it. Lock held."""
        task = getattr(entry, kind)
        if task is None or self._expired(task):
            task = self._submit(function, entry.target.key)
            setattr(entry, kind, task)
        return task

    # Pages (section 3)

    def index_page(self, visitor: Visitor) -> bytes:
        with self.lock:
            remaining = self.limits.remaining(visitor.key)
            recent = list(self.recent.get(visitor.identity, ())) if visitor.identity else []
            lines = [self._recent_line(target, name) for target, name in recent]
        return pages.index(self.config.node_name, visitor.identity, self.config.access, remaining,
                           self.config.max_file_size, lines, self.address, self.config.auto_pin_max_size,
                           self.config.html_pages)

    def _recent_line(self, target: Target, name: str | None) -> str:
        """A recent fetch (name: the download name asked for), linked to its page, with its state. Lock held."""
        label = name or (target.names[-1] if target.names else target.cid)
        entry = self.entries.get(target.key)
        state = ""
        if entry is not None and entry.info is not None and entry.info.status == DONE:
            cid = str(entry.info.result.cid)
            fetch = self.fetches.get(cid)
            if self.spool.find(cid):
                state = "ready"
            elif fetch is not None and fetch.status in (QUEUED, RUNNING):
                state = "fetching"
            elif fetch is not None and fetch.status == FAILED:
                state = "failed"
            else:
                state = "expired"
        return pages.recent_line(micron.target_link(label, target, **({"o": name} if name else {})), state)

    def get_page(self, visitor: Visitor, data, html: bool = False) -> bytes:
        """/page/get.mu (or /page/index.mu with `cid`): the target's state (section 5). html: the page is
        wanted as HTML, so print=1 shows the file as HTML.

        While a lookup, listing or fetch is running, the page waits for it up to page_wait seconds, so content
        the rnfs node has (e.g. pinned) is shown, or downloadable, without a Refresh. A file asked for with
        print=1 or base64=1 is then sent as it is (a Raw response), if it can be."""
        value = target_value(data)
        try:
            target = parse_target(value)
        except ValueError as e:
            return pages.invalid_target(str(e), self.address)
        name, note = output_name(data)
        action = request_value(data, "action")
        show = "base64" if flag(data, "base64") else "print" if flag(data, "print") else None
        try:
            page_no = int(request_value(data, "page") or 1)
        except ValueError:
            page_no = 1
        deadline = self.wait_clock() + self.config.page_wait
        while True:
            with self.lock:
                page, running = self._target_page(visitor, target, action, page_no, name, note, show, html)
            remaining = deadline - self.wait_clock()
            if running is None or remaining <= 0:
                return page() if callable(page) else page  # a callable: read the file without the lock
            if not self.waiting.acquire(blocking=False):
                return page() if callable(page) else page  # too many waiting: no more threads held up
            try:
                running.event.wait(remaining)
            finally:
                self.waiting.release()
            if action in ("fetch", "retry"):
                action = None  # asked for once; a view is still shown once the file is ready

    def _target_page(self, visitor: Visitor, target: Target, action: str | None, page_no: int, name: str | None,
                     note: str, show: str | None = None, html: bool = False) -> tuple[bytes, Task | None]:
        """The target's page now, and the task it waits on, if any. Lock held."""
        keep = {"o": name} if name else {}
        again, refresh = asked_again(keep, action, show)
        entry = self._entry(target)
        if action == "retry":
            for kind in ("info", "listing"):
                task = getattr(entry, kind)
                if task is not None and task.status == FAILED:
                    setattr(entry, kind, None)
        info = self._ensure(entry, "info", self.api.info)
        if info is None:
            return pages.busy(target, refresh), None
        if info.status in (QUEUED, RUNNING):
            return pages.waiting(target, "Looking up", self._waiting(info), refresh), info
        if info.status == FAILED:
            return pages.failure(target, "Lookup", info.error, self._retry_after(info), "retry", again), None
        if info.result.type == "directory":
            return self._directory_page(entry, page_no)
        return self._file_page(visitor, entry, info.result, action, name, note, show, html, page_no)

    def _retry_after(self, task: Task) -> str:
        return micron.duration(max(1.0, self.config.failure_ttl - (self.clock() - task.finished)))

    def _directory_page(self, entry: Entry, page_no: int) -> tuple[bytes, Task | None]:
        listing = self._ensure(entry, "listing", self._list)
        if listing is None:
            return pages.busy(entry.target, {}), None
        if listing.status in (QUEUED, RUNNING):
            return pages.waiting(entry.target, "Listing", self._waiting(listing), {}), listing
        if listing.status == FAILED:
            page = pages.failure(entry.target, "Listing", listing.error, self._retry_after(listing), "retry", {})
            return page, None
        return pages.directory(entry.target, listing.result, page_no, self.config.listing_page_size), None

    def _file_page(self, visitor: Visitor, entry: Entry, info: rnfs.Info, action: str | None, asked: str | None,
                   note: str, show: str | None = None, html: bool = False,
                   page_no: int = 1) -> tuple[bytes, Task | None]:
        """A file: its details, and a download link, the fetch's state, or a Fetch link; and the fetch the page
        waits on. asked is the download name asked for (o=), note why one asked for was refused. A file of up
        to auto_pin_max_size is fetched (and pinned) without being asked. show: "print" or "base64", to send
        the ready file as it is instead (see _shown). Asking to see a file (print=1, base64=1, View) also asks
        for it to be fetched, if it is small enough to be shown so. Lock held."""
        target, cid = entry.target, str(info.cid)
        size = stored_size(info)
        name = download_name(target, cid, asked)
        keep = {"o": asked} if asked else {}
        again, refresh = asked_again(keep, action, show)
        if show == "base64":
            wanted = 0 < size <= self.config.base64_max_size
        else:
            wanted = ((show == "print" or action == "view") and not info.sealed
                      and 0 < size <= self.config.view_max_size)
        wanted = wanted or action == "fetch"
        kind = page_kind(name, target)
        spooled = self.spool.find(cid)
        if spooled is not None:
            self.pins.touch(cid)
            path = self._download_path(target, cid, name)
            if show == "base64":
                if 0 < size <= self.config.base64_max_size:
                    return (lambda: self._base64(spooled, target, keep)), None
                note = f"Too big for base64. The limit is {micron.size(self.config.base64_max_size)}."
            elif show == "print" or action == "view":
                text, per_page = self._view_text(cid, info, spooled, size), self.config.view_page_lines
                if text is not None and show == "print":
                    return Raw(printed(text, kind, name, html, target, page_no, per_page, keep)), None
                if text is not None:
                    return pages.view(target, name, text, kind == "mu", path, keep, page_no, per_page), None
                note = f"Can't show this one. Only text files up to {micron.size(self.config.view_max_size)} can be."
            view, text = "", self._viewable(cid, info, spooled, size)
            if text:
                view = pages.view_link(target, keep, html=kind == "html" and self.config.html_pages)
            links = pages.file_links(path, name, view)
            own = cid if target.names and self.pins.holds(cid) else None  # pinned on its own: its CID gets it
            return pages.file(target, info, size, links, name, note, asked, text, own), None
        text = bool(self.viewable.get(cid))  # known once it has been fetched
        fetch = self.fetches.get(cid)
        if fetch is not None and (self._expired(fetch) or fetch.status == DONE
                                  or (fetch.status == FAILED and action == "fetch")):
            # A failure forgotten or retried, or a fetch whose file has been removed from the spool since
            del self.fetches[cid]
            fetch = None
        if fetch is None and (wanted or self._auto_pin(size)):
            refusal = self._start_fetch(visitor, target, cid, size, name, asked)
            if refusal is None:
                fetch = self.fetches[cid]
            elif wanted:
                return pages.file(target, info, size, pages.refusal(refusal), name, note, asked, text), None
            # An automatic fetch that can't start now leaves the page as it was: a Fetch link, or why not
        if fetch is not None and fetch.status in (QUEUED, RUNNING):
            elapsed = micron.duration(self.clock() - fetch.created)
            commands = pages.rnfs_commands(target, asked, info.sealed, text)
            return pages.fetching(target, elapsed, self._waiting(fetch), refresh, commands), fetch
        if fetch is not None:
            commands = pages.rnfs_commands(target, asked, info.sealed, text)
            page = pages.failure(target, "Fetch", fetch.error, self._retry_after(fetch), "fetch", again, commands)
            return page, None
        refusal = self._fetch_refusal(visitor, size)
        action_text = pages.fetch_link(target, keep) if refusal is None else pages.refusal(refusal)
        return pages.file(target, info, size, action_text, name, note, asked, text), None

    def _view_text(self, cid: str, info: rnfs.Info, path: str, size: int) -> str | None:
        """The spooled file's text if it can be shown as a page: within view_max_size, not sealed (its content is
        encrypted), UTF-8 without NULs. Remembered for _viewable, unless it can't be read (e.g. removed from the
        spool meanwhile). Lock held (a small read)."""
        text = None
        if 0 < size <= self.config.view_max_size and not info.sealed:
            try:
                with open(path, "rb") as f:
                    data = f.read(self.config.view_max_size + 1)
            except OSError:
                return None
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
            if len(data) > self.config.view_max_size or (text is not None and "\0" in text):
                text = None
        self.viewable[cid] = text is not None
        self.viewable.move_to_end(cid)
        while len(self.viewable) > MAX_DOWNLOAD_PATHS:
            self.viewable.popitem(last=False)
        return text

    def _list(self, key: str) -> list[rnfs.Entry]:
        """Worker: a directory's listing, kept within MAX_LISTED_ENTRIES by dropping the least recently listed."""
        entries = self.api.ls(key)
        with self.lock:
            self.listed.pop(key, None)
            while self.listed and sum(self.listed.values()) + len(entries) > MAX_LISTED_ENTRIES:
                old, _ = self.listed.popitem(last=False)
                kept = self.entries.get(old)
                if kept is not None and kept.listing is not None and kept.listing.status == DONE:
                    kept.listing = None
            self.listed[key] = len(entries)
        return entries

    def _base64(self, path: str, target: Target, keep: dict) -> bytes:
        """The spooled file as base64 text (base64=1), read without the lock."""
        try:
            with open(path, "rb") as f:
                data = f.read(self.config.base64_max_size + 1)
        except OSError:
            return pages.not_ready(target, keep)  # removed meanwhile
        if len(data) > self.config.base64_max_size:  # larger than the daemon said: never sent cut short
            return pages.not_ready(target, keep)
        return Raw(base64.b64encode(data))

    def _viewable(self, cid: str, info: rnfs.Info, path: str, size: int) -> bool:
        """True if the spooled file can be shown as a page; read only the first time (a CID's content never
        changes), so the file's page doesn't read it each time it is shown. Lock held."""
        known = self.viewable.get(cid)
        if known is None:
            return self._view_text(cid, info, path, size) is not None
        self.viewable.move_to_end(cid)
        return known

    def _auto_pin(self, size: int) -> bool:
        """True if a file this size is fetched without being asked, and pinned (auto_pin_max_size; 0: never)."""
        return 0 < self.config.auto_pin_max_size and size <= self.config.auto_pin_max_size

    def _fetch_refusal(self, visitor: Visitor, size: int) -> str | None:
        """Why visitor can't have a file of size fetched now, or None. Lock held."""
        if size > self.config.max_file_size:
            return f"Too big. This gateway takes files up to {micron.size(self.config.max_file_size)}."
        if size > self.config.spool_max:
            return "Too big for this gateway's storage."
        return self.limits.refusal(visitor.key)

    def _start_fetch(self, visitor: Visitor, target: Target, cid: str, size: int, name: str,
                     asked: str | None) -> str | None:
        """Start fetching target into the spool, or say why not. Lock held."""
        refusal = self._fetch_refusal(visitor, size)
        if refusal is not None:
            return refusal
        if self.pending >= MAX_PENDING:
            return "The gateway is busy. Try again in a moment."
        if not self.spool.reserve(cid, size):
            return "No room for this file right now. Try again later."
        pin = self._auto_pin(size) or bool(target.names)  # a file inside a folder: pinned on its own
        task = self._submit(self._fetch, visitor.key, target.key, cid, name, size, pin)
        if task is None:
            self.spool.unreserve(cid)
            return "The gateway is busy. Try again in a moment."
        self.limits.start(visitor.key)
        self.fetches[cid] = task
        if visitor.identity is not None:
            recent = self.recent.setdefault(visitor.identity, deque(maxlen=RECENT_FETCHES))
            if (target, asked) in recent:
                recent.remove((target, asked))
            recent.appendleft((target, asked))
            self.recent.move_to_end(visitor.identity)
            while len(self.recent) > MAX_VISITORS:
                self.recent.popitem(last=False)
        log(f"fetching {target.key} ({size} bytes) for {visitor.key}", RNS.LOG_VERBOSE)
        return None

    def _fetch(self, visitor_key: str, key: str, cid: str, name: str, size: int, pin: bool):
        """Worker: have the daemon fetch the target, raw, into the spool. With pin (a file of up to
        auto_pin_max_size, or any inside a folder), it is pinned on the rnfs node first, within auto_pin_quota: so
        it stays there, is served at once next time, and is provided under its own CID, which then fetches it
        alone (rnfs pins a path's file as itself, not its folder)."""
        try:
            directory = self.spool.directory_for(cid)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            if pin and self.pins.pin(key, cid, size):
                log(f"pinned {key}", RNS.LOG_VERBOSE)
            if self.spool.find(cid) is None:
                try:
                    self.api.get(key, os.path.join(directory, name), raw=True)
                except rnfs.ApiError:
                    if self.spool.find(cid) is None:  # else another fetch (e.g. before a restart) wrote it
                        raise
            path = self.spool.find(cid)
            if path is None:
                raise rnfs.ApiError("the rnfs node reported the fetch complete, but wrote no file", 500)
            self.spool.touch(path)
            log(f"fetched {key}", RNS.LOG_VERBOSE)
        except Exception:
            self.spool.remove_empty(cid)
            raise
        finally:
            with self.lock:
                self.limits.finish(visitor_key)
            self.spool.unreserve(cid)

    # Downloads (section 6)

    def _download_path(self, target: Target, cid: str, name: str) -> str:
        """The per-file path serving cid's spooled file as name, registered if new. Lock held."""
        path = f"/file/{cid}/{name if PATH_NAME.fullmatch(name) else cid}"
        if path not in self.download_paths:
            self.register_path(path)
        self.download_paths[path] = (target, cid, name)
        self.download_paths.move_to_end(path)
        while len(self.download_paths) > MAX_DOWNLOAD_PATHS:
            old, _ = self.download_paths.popitem(last=False)
            self.deregister_path(old)
        return path

    def register_spooled(self):
        """Register download paths for the files already in the spool (e.g. after a restart)."""
        with self.lock:
            for cid, path in self.spool.files():
                self._download_path(Target(cid), cid, os.path.basename(path))

    def download(self, path: str, identity_hash: bytes | None, owner=None):
        """A per-file download path: the spooled file, or a page saying why not."""
        visitor = self.visitor(identity_hash)
        if not visitor.allowed:
            return pages.refused(visitor.identity)
        with self.lock:
            item = self.download_paths.get(path)
        if item is None:
            return micron.page("Not found", "There's no such file here.")
        target, cid, name = item
        self.pins.touch(cid)
        spooled = self.spool.find(cid)
        if spooled is None:
            return pages.not_ready(target, {})
        try:
            file = self.spool.open_for_send(spooled, owner)
        except OSError:
            return pages.not_ready(target, {})
        log(f"sending {target.key} to {visitor.key}", RNS.LOG_VERBOSE)
        return [file, {"name": name.encode("utf-8")}]

    def file_response(self, visitor: Visitor, data, owner):
        """/file/get: the spooled file, or a page saying why not."""
        value = target_value(data)
        try:
            target = parse_target(value)
        except ValueError as e:
            return pages.invalid_target(str(e), self.address)
        asked, _ = output_name(data)
        keep = {"o": asked} if asked else {}
        with self.lock:
            entry = self.entries.get(target.key)
            if entry is not None and entry.info is not None and entry.info.status == DONE:
                cid = str(entry.info.result.cid)
            elif not target.names:
                cid = target.cid  # a bare CID is its own target CID: downloadable even after a restart
            else:
                cid = None
            path = self.spool.find(cid) if cid is not None else None
        if path is None:
            return pages.not_ready(target, keep)
        try:
            file = self.spool.open_for_send(path, owner)
        except OSError:
            return pages.not_ready(target, keep)  # removed meanwhile
        log(f"sending {target.key} to {visitor.key}", RNS.LOG_VERBOSE)
        return [file, {"name": download_name(target, cid, asked).encode("utf-8")}]

    # Upkeep

    def maintain(self):
        """Expire spool files, save pin records and forget finished state; run periodically."""
        self.spool.clean()
        self.pins.save()
        with self.lock:
            self.limits.prune()
            for cid, task in list(self.fetches.items()):
                if task.status == DONE or self._expired(task):
                    del self.fetches[cid]


def asked_again(keep: dict, action: str | None, show: str | None) -> tuple[dict, dict]:
    """The link variables that ask for the same again: for a Try again link (which sets its own action), and for
    a Refresh link, which also keeps action=view. So a file asked for with print=1, base64=1 or View is shown
    once its fetch is done, not its page."""
    again = {**keep, show: "1"} if show else keep
    return again, {**again, "action": "view"} if action == "view" else again


def page_kind(name: str, target: Target) -> str:
    """How a file is shown as a page, by its download or path name: "mu" (Micron), "html", or "text"."""
    names = [n.lower() for n in (name, *target.names[-1:])]
    if any(n.endswith(".mu") for n in names):
        return "mu"
    if any(n.endswith((".html", ".htm")) for n in names):
        return "html"
    return "text"


def printed(text: str, kind: str, name: str, html: bool, target: Target, page_no: int = 1, per_page: int = 50,
            keep: dict | None = None) -> bytes:
    """A text file as the page itself (print=1): Micron as Micron and HTML as HTML, when the page is asked for
    as that (a client picks how to render a page by its path: .mu or .html), else shown as written: as Micron,
    per_page lines at a time (page_no), with links to the other pages. What a client would load by itself
    (partials, images) is left out, as everywhere."""
    if html and kind == "html":
        return micron.without_auto_loading(text).encode("utf-8")
    if html:
        if kind == "mu":
            return webpage.to_html(micron.hosted_micron(text, directives=True))
        return webpage.text_page(text, name)
    if kind == "mu":
        return micron.hosted_micron(text, directives=True).encode("utf-8")
    return (pages.text_page(target, text, page_no, per_page, {"print": "1", **(keep or {})}) + "\n").encode("utf-8")


def stored_size(info: rnfs.Info) -> int:
    """The bytes a raw fetch writes: a sealed file's stored (encrypted) size, not its plaintext size."""
    return info.stored_size if info.stored_size is not None else info.size


def download_name(target: Target, cid: str, asked: str | None = None) -> str:
    """The name asked for (o=, already checked), else the target's last path name if rnfs's default-name rule
    allows it, else the target's CID."""
    if asked:
        return asked
    return safe_default_name(target.names[-1], cid) if target.names else cid
