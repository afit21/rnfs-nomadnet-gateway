"""The gateway on Reticulum (specification sections 2 and 3): its own identity, one IN/SINGLE destination
`nomadnetwork.node` announced with the node name like NomadNet nodes, and request handlers for its fixed paths.

Handlers are registered ALLOW_ALL and check access themselves (section 4), so a refused visitor gets a page
explaining why instead of a request that silently fails.
"""
import os
import threading

import RNS

from rnfs_nomadnet_gateway.config import Config
from rnfs_nomadnet_gateway.gateway import Gateway, log
from rnfs_nomadnet_gateway.micron import FILE_PATH, GET_HTML, GET_PAGE, INDEX_HTML, INDEX_PAGE, page

APP_NAME = "nomadnetwork"
ASPECT = "node"
IDENTITY_FILE = "identity"
FILE_COMPRESS_LIMIT = 32_000_000  # as NomadNet: compress downloads up to this size
MAINTENANCE_INTERVAL = 60.0
# Requests are read whole before a handler (and its access check) sees them: anyone could otherwise send one of
# any size. The gateway's are a few short variables (each read up to MAX_VALUE characters)
MAX_REQUEST_SIZE = 64 * 1024


def load_identity(path: str) -> RNS.Identity:
    """The gateway's identity from path, made (owner-only) if there is none. Raises OSError or ValueError."""
    if os.path.exists(path):
        identity = RNS.Identity.from_file(path)
        if identity is None:
            raise ValueError(f"{path} isn't a valid RNS identity")
        return identity
    identity = RNS.Identity()
    temp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(identity.get_private_key())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    log(f"made a new identity in {path}")
    return identity


def describe_data(data) -> str:
    """A request's data for the log: its keys and their values' types, values cut short."""
    if not isinstance(data, dict):
        return f"{type(data).__name__} {str(data)[:80]!r}" if data is not None else "none"
    parts = []
    for key, value in list(data.items())[:16]:
        shown = value[:100] if isinstance(value, (str, bytes)) else value
        parts.append(f"{key!r}: {type(value).__name__} {shown!r}"[:160])
    return "{" + ", ".join(parts) + ("" if len(data) <= 16 else ", …") + "}"


def destination_hash(identity: RNS.Identity) -> bytes:
    """The gateway's node address, as NomadNet clients list it."""
    return RNS.Destination.hash(identity, APP_NAME, ASPECT)


class GatewayNode:
    """The RNS destination serving a Gateway."""

    def __init__(self, config: Config, gateway: Gateway, identity: RNS.Identity):
        self.config = config
        self.gateway = gateway
        self.identity = identity
        self.stopping = threading.Event()
        self.destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        self.destination.set_max_request_size(MAX_REQUEST_SIZE)
        self.destination.set_link_established_callback(self._link_established)
        # The HTML version of the pages (html_pages): a client renders a page by its path's ending
        for path in (INDEX_PAGE, GET_PAGE) + ((INDEX_HTML, GET_HTML) if config.html_pages else ()):
            self.destination.register_request_handler(path, response_generator=self._handle,
                                                      allow=RNS.Destination.ALLOW_ALL)
        self.destination.register_request_handler(FILE_PATH, response_generator=self._handle,
                                                  allow=RNS.Destination.ALLOW_ALL,
                                                  auto_compress=FILE_COMPRESS_LIMIT)
        # Each ready file also gets its own path, registered as it is offered (and for the spool at startup)
        gateway.register_path = self._register_download
        gateway.deregister_path = self.destination.deregister_request_handler
        gateway.register_spooled()
        gateway.address = self.destination.hash.hex()

    @property
    def hash(self) -> bytes:
        return self.destination.hash

    def start(self):
        """Announce now and every announce_interval, and keep the spool tidy, on background threads."""
        threading.Thread(target=self._announce_loop, name="gateway-announce", daemon=True).start()
        threading.Thread(target=self._maintenance_loop, name="gateway-maintenance", daemon=True).start()

    def stop(self):
        self.stopping.set()

    def announce(self):
        self.destination.announce(app_data=self.config.node_name.encode("utf-8"))
        log(f"announced {RNS.prettyhexrep(self.hash)} as {self.config.node_name}", RNS.LOG_VERBOSE)

    def _announce_loop(self):
        while not self.stopping.is_set():
            try:
                self.announce()
            except Exception as e:  # noqa: BLE001 - try again next time
                log(f"announce failed: {e!r}", RNS.LOG_ERROR)
            self.stopping.wait(self.config.announce_interval)

    def _maintenance_loop(self):
        while not self.stopping.wait(MAINTENANCE_INTERVAL):
            try:
                self.gateway.maintain()
            except Exception as e:  # noqa: BLE001 - try again next time
                log(f"spool maintenance failed: {e!r}", RNS.LOG_ERROR)

    def _link_established(self, link: RNS.Link):
        # RNS closes a sent file itself, but not one whose transfer failed: those are closed with the link
        link_id = link.link_id
        link.set_link_closed_callback(lambda _link: self.gateway.spool.release(link_id))

    def _register_download(self, path: str):
        self.destination.register_request_handler(path, response_generator=self._handle_download,
                                                  allow=RNS.Destination.ALLOW_ALL,
                                                  auto_compress=FILE_COMPRESS_LIMIT)

    def _handle_download(self, path, data, request_id, link_id, remote_identity, requested_at):
        """A per-file download path (/file/<cid>/<name>)."""
        identity_hash = remote_identity.hash if remote_identity is not None else None
        who = identity_hash.hex() if identity_hash else "an unidentified visitor"
        log(f"request for {path} from {who}", RNS.LOG_VERBOSE)
        try:
            return self.gateway.download(path, identity_hash, owner=link_id)
        except Exception as e:  # noqa: BLE001 - a visitor gets a page, the details go to the log
            log(f"request for {path} failed: {e!r}", RNS.LOG_ERROR)
            return page("Error", "The gateway had an internal error.")

    def _handle(self, path, data, request_id, link_id, remote_identity, requested_at):
        """Every request: identified visitors carry remote_identity (Link.identify)."""
        identity_hash = remote_identity.hash if remote_identity is not None else None
        # What the client sent, for working out what a client does differently (shown with -v)
        who = identity_hash.hex() if identity_hash else "an unidentified visitor"
        log(f"request for {path} from {who}, data: {describe_data(data)}", RNS.LOG_VERBOSE)
        try:
            return self.gateway.respond(path, data, identity_hash, owner=link_id)
        except Exception as e:  # noqa: BLE001 - a visitor gets a page, the details go to the log
            log(f"request for {path} failed: {e!r}", RNS.LOG_ERROR)
            return page("Error", "The gateway had an internal error.")
