"""Settings (specification sections 7 and 8), from `<gateway dir>/config.toml`.

Times are in seconds and sizes in bytes, as in rnfs.toml.
"""
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

DEFAULT_GATEWAY_DIR = "~/.rnfs-nomadnet-gateway"
SETTINGS_FILE = "config.toml"
DEFAULT_RNFS_DIR = "~/.rnfs"
ACCESS_MODES = ("whitelist", "open")
MAX_DURATION = 30 * DAY  # waited on by threads; Windows can wait at most ~49 days at once

# name: (minimum, maximum or None)
WHOLE_NUMBERS = {
    "max_file_size": (0, None), "max_fetches": (1, None), "max_fetches_per_visitor": (1, None),
    "fetches_per_hour": (1, None), "max_workers": (1, 64), "listing_page_size": (1, 1000), "spool_max": (0, None),
    "auto_pin_max_size": (0, None), "auto_pin_quota": (0, None), "view_max_size": (0, 16 * MIB),
    "view_page_lines": (1, 1000), "base64_max_size": (0, 16 * MIB),
}
FLAGS = ("html_pages",)  # true or false
MAX_PAGE_WAIT = 10.0  # seconds: a page handler holds up the RNS thread that called it while it waits
DURATIONS = ("announce_interval", "spool_ttl", "failure_ttl")  # seconds, above 0
_HEX_ID = re.compile(r"[0-9a-fA-F]{32}")

EXAMPLE = '''\
# rnfs NomadNet gateway settings. Times are in seconds, sizes in bytes. Uncomment to change a default.

# node_name = "rnfs gateway"            # announced name
# announce_interval = 21600             # 6 h
# rnfs_dir = "~/.rnfs"                  # the rnfs daemon's --dir: where its API token and rnfs.toml are
# api_port = 8788                       # default: api_port from rnfs.toml, else 8788
# reticulum_dir = "~/.reticulum"        # default: RNS's own (a running shared instance is found anyway)
# access = "whitelist"                  # "whitelist" or "open"
# allowed_identities = []               # visitors' RNS identity hashes, 32 hex characters each
# spool_max = 1073741824                # 1 GiB of fetched files kept for download
# spool_ttl = 604800                    # 7 days since a file was last fetched or downloaded

# max_file_size = 104857600             # 100 MiB: the largest file a visitor may have fetched
# max_fetches = 2                       # fetches running at once
# max_fetches_per_visitor = 1           # fetches one visitor may have running
# fetches_per_hour = 10                 # fetches one visitor may start per hour
# max_workers = 2                       # lookups, listings and fetches run against the rnfs API at once
# listing_page_size = 50                # directory entries per page
# failure_ttl = 300                     # how long a failed lookup or fetch is remembered
# auto_pin_max_size = 1048576           # 1 MiB: smaller files are fetched and pinned without being asked; 0: never
# page_wait = 2                         # seconds a page waits for a lookup or fetch before saying "Refresh"
# auto_pin_quota = 4294967296           # 4 GiB: the gateway's pins (small files, files fetched from folders);
#                                       # the least recently used are released first; 0: none
# view_max_size = 5242880               # 5 MiB: text files (.txt, .md, .mu, …) up to this can be viewed as a
#                                       # page, or shown as the page itself (print=1); 0: never
# view_page_lines = 50                  # plain text over this many lines is shown a page of them at a time
# base64_max_size = 1048576             # 1 MiB: files up to this can be had as base64 text (base64=1); 0: never
# html_pages = true                     # also serve every page as HTML, at /page/index.html and /page/get.html
'''


def _is_number(value) -> bool:
    """True if value is a finite int or float; not a bool, which Python counts as an int."""
    return type(value) in (int, float) and math.isfinite(value)


@dataclass
class Config:
    """The gateway's settings."""

    gateway_dir: str = DEFAULT_GATEWAY_DIR
    node_name: str = "rnfs gateway"
    announce_interval: float = 6 * HOUR
    rnfs_dir: str = DEFAULT_RNFS_DIR  # the rnfs daemon's --dir (install.sh sets it to the running daemon's)
    api_port: int | None = None  # None: api_port from rnfs.toml, else rnfs's default
    reticulum_dir: str | None = None  # None: RNS's own, the one rnsd uses
    access: str = "whitelist"
    allowed_identities: list[str] = field(default_factory=list)
    spool_max: int = GIB
    spool_ttl: float = 7 * DAY
    # Limits (section 7)
    max_file_size: int = 100 * MIB
    max_fetches: int = 2
    max_fetches_per_visitor: int = 1
    fetches_per_hour: int = 10
    max_workers: int = 2
    listing_page_size: int = 50
    failure_ttl: float = 5 * MINUTE
    auto_pin_max_size: int = MIB  # files up to this are fetched and pinned without being asked; 0: never
    page_wait: float = 2.0  # seconds a page waits for a lookup or fetch to finish before saying "Refresh"
    auto_pin_quota: int = 4 * GIB  # bytes the gateway's own pins may hold; least recently used released first
    view_max_size: int = 5 * MIB  # text files up to this can be viewed as a page (or print=1); 0: never
    view_page_lines: int = 50  # plain text is viewed (or printed, on NomadNet) this many lines at a time
    base64_max_size: int = MIB  # files up to this can be had as base64 text (base64=1); 0: never
    html_pages: bool = True  # also serve every page as HTML (.html paths, or html=1)

    def validate(self):
        """Raise ValueError for a setting of the wrong type or out of range.

        type() rather than isinstance(): true is an int to Python, but not a count anyone means.
        """
        if not isinstance(self.node_name, str) or not self.node_name.strip():
            raise ValueError("node_name must be a non-empty string")
        if len(self.node_name.encode("utf-8")) > 128:
            raise ValueError("node_name must be at most 128 bytes of UTF-8")
        for name, (minimum, maximum) in WHOLE_NUMBERS.items():
            value = getattr(self, name)
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                limits = f"{minimum}-{maximum}" if maximum is not None else f"at least {minimum}"
                raise ValueError(f"{name} must be a whole number, {limits}")
        for name in FLAGS:
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be true or false")
        for name in DURATIONS:
            value = getattr(self, name)
            if not _is_number(value) or value <= 0 or value > MAX_DURATION:
                raise ValueError(f"{name} must be a number of seconds, above 0 and at most {MAX_DURATION}")
        if not _is_number(self.page_wait) or not 0 <= self.page_wait <= MAX_PAGE_WAIT:
            raise ValueError(f"page_wait must be a number of seconds, 0-{MAX_PAGE_WAIT:g}")
        if self.api_port is not None and (type(self.api_port) is not int or not 1 <= self.api_port <= 65535):
            raise ValueError("api_port must be a whole number, 1-65535")
        if not isinstance(self.gateway_dir, str) or not self.gateway_dir:
            raise ValueError("gateway_dir must be a directory path")
        if not isinstance(self.rnfs_dir, str) or not self.rnfs_dir:
            raise ValueError("rnfs_dir must be a directory path")
        if self.reticulum_dir is not None and (not isinstance(self.reticulum_dir, str) or not self.reticulum_dir):
            raise ValueError("reticulum_dir must be a directory path")
        if self.access not in ACCESS_MODES:
            raise ValueError("access must be \"whitelist\" or \"open\"")
        if not isinstance(self.allowed_identities, list) or not all(
                isinstance(value, str) and _HEX_ID.fullmatch(value.strip().strip("<>"))
                for value in self.allowed_identities):
            raise ValueError("allowed_identities must be a list of RNS identity hashes (32 hex characters each)")

    @property
    def allowed(self) -> frozenset[bytes]:
        """allowed_identities as identity hashes; `<…>` brackets, as RNS prints hashes, are allowed."""
        return frozenset(bytes.fromhex(value.strip().strip("<>")) for value in self.allowed_identities)

    def path(self, *names: str) -> str:
        """A path inside the gateway dir."""
        return os.path.join(os.path.expanduser(self.gateway_dir), *names)

    @classmethod
    def load(cls, gateway_dir: str = DEFAULT_GATEWAY_DIR, **overrides) -> "Config":
        """Settings from `<gateway_dir>/config.toml` if it exists, then overrides (None values ignored),
        validated. Raises ValueError."""
        values: dict = {}
        path = os.path.join(os.path.expanduser(gateway_dir), SETTINGS_FILE)
        try:
            with open(path, "rb") as f:
                values.update(tomllib.load(f))
        except FileNotFoundError:
            pass
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"can't read {path}: {e}") from None
        known = {f.name for f in fields(cls)} - {"gateway_dir"}
        unknown = values.keys() - known
        if unknown:
            raise ValueError(f"unknown settings in {path}: {', '.join(sorted(unknown))}")
        values.update({k: v for k, v in overrides.items() if v is not None})
        config = cls(gateway_dir=gateway_dir, **values)
        config.validate()
        return config


def write_example(gateway_dir: str):
    """Write a config.toml with every setting commented out, unless one exists."""
    path = os.path.join(os.path.expanduser(gateway_dir), SETTINGS_FILE)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(EXAMPLE)
