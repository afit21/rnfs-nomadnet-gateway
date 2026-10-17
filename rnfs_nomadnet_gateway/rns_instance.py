"""Attaching to a shared Reticulum instance that was started from another config directory.

RNS attaches a program to a running shared instance (rnsd, or the first RNS program started, such as the rnfs
daemon) by its instance name, whatever config directory the program uses. But the shared instance answers its
local RPC calls (whether an identity is blackholed, which Link.identify needs; next hops; path timeouts) only to
clients that prove its RPC key, which RNS derives from the transport identity in the instance's own config
directory (or sets with `rpc_key` in its config). A program using another directory, such as the gateway with
RNS's default while the rnfs daemon was started with --rnsconfig, attaches but has every RPC call refused.

So once attached, the gateway checks its key, and if the instance refuses it, looks for the right one: in the
config directory the instance's process was started with, in rnfs's reticulum_dir, and in RNS's default
directories, all of which belong to this user. Each candidate key is tried against the instance; a wrong one
costs a line in the instance's log.
"""
import multiprocessing
import multiprocessing.connection
import os

import rnfs.config
import RNS
import RNS.vendor.umsgpack as umsgpack
from RNS.vendor.configobj import ConfigObj

DEFAULT_DIRS = ("/etc/reticulum", "~/.config/reticulum", "~/.reticulum")
MAX_TRIES = 12  # candidate keys tried at most: each wrong one logs an error in the shared instance
PROC = "/proc"


def rpc_accepts(reticulum, key: bytes) -> bool:
    """True if the shared instance accepts key for RPC (checked with a harmless call)."""
    try:
        connection = multiprocessing.connection.Client(reticulum.rpc_addr, family=reticulum.rpc_type, authkey=key)
    except (multiprocessing.AuthenticationError, OSError, EOFError):
        return False
    try:
        # A call the instance answers, so it doesn't log a broken connection
        connection.send_bytes(umsgpack.packb({"get": "is_blackholed", "identity_hash": bytes(16)}))
        connection.recv_bytes()
    except (OSError, EOFError):
        pass  # the key was accepted; an older instance may not know the call
    finally:
        connection.close()
    return True


def keys_in(directory: str) -> list[bytes]:
    """The RPC keys a Reticulum config directory gives: its config's rpc_key, else its transport identity's."""
    keys = []
    config_path = os.path.join(directory, "config")
    if os.path.isfile(config_path):
        try:
            value = ConfigObj(config_path).get("reticulum", {}).get("rpc_key")
            if value:
                keys.append(bytes.fromhex(value))
        except Exception:  # noqa: BLE001 - an unreadable config just gives no key
            pass
    identity_path = os.path.join(directory, "storage", "transport_identity")
    if os.path.isfile(identity_path):
        try:
            with open(identity_path, "rb") as f:
                private_key = f.read(1024)
            identity = RNS.Identity.from_bytes(private_key)
            if identity is not None:
                keys.append(RNS.Identity.full_hash(identity.get_private_key()))
        except (OSError, ValueError):
            pass
    return keys


def is_config_dir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "storage", "transport_identity"))


def dirs_in_args(args: list[str], cwd: str) -> list[str]:
    """Reticulum config directories named in a command line (`--config DIR`, `--rnsconfig=DIR`, …): any argument,
    or option value, that is a directory holding a transport identity."""
    found = []
    for arg in args:
        for value in (arg, arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else None):
            if not value or value.startswith("-"):
                continue
            path = os.path.expanduser(value)
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            path = os.path.normpath(path)
            if os.path.isdir(path) and is_config_dir(path) and path not in found:
                found.append(path)
    return found


def _process_args(pid: str) -> tuple[list[str], str] | None:
    """A process's command line and working directory, if this user may read them."""
    try:
        with open(os.path.join(PROC, pid, "cmdline"), "rb") as f:
            args = [arg.decode("utf-8", "replace") for arg in f.read().split(b"\0") if arg]
        return args, os.readlink(os.path.join(PROC, pid, "cwd"))
    except OSError:
        return None


def _socket_owners(socket_name: str) -> list[str]:
    """PIDs (of this user) holding the listening abstract Unix socket @<socket_name>."""
    inodes = set()
    try:
        with open(os.path.join(PROC, "net", "unix"), encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 8 and fields[7] == "@" + socket_name:
                    inodes.add(fields[6])
    except OSError:
        return []
    links = {f"socket:[{inode}]" for inode in inodes}
    owners = []
    for pid in _pids():
        try:
            fds = os.listdir(os.path.join(PROC, pid, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(PROC, pid, "fd", fd)) in links:
                    owners.append(pid)
                    break
            except OSError:
                continue
    return owners


def _pids() -> list[str]:
    """Other processes of this user."""
    try:
        names = os.listdir(PROC)
    except OSError:
        return []
    me, uid = str(os.getpid()), os.getuid()
    pids = []
    for name in names:
        if name.isdigit() and name != me:
            try:
                if os.stat(os.path.join(PROC, name)).st_uid == uid:
                    pids.append(name)
            except OSError:
                continue
    return pids


def rnfs_reticulum_dir(rnfs_dir: str) -> str | None:
    """reticulum_dir from rnfs.toml: the rnfs daemon is often the shared instance."""
    try:
        return rnfs.config.Config.load(rnfs_dir).reticulum_root
    except (OSError, ValueError, TypeError):
        return None


def candidate_dirs(reticulum, rnfs_dir: str) -> list[str]:
    """Config directories the shared instance may use, the likeliest first."""
    dirs: list[str] = []

    def add(paths):
        for path in paths:
            path = os.path.normpath(os.path.expanduser(path))
            if path not in dirs and is_config_dir(path):
                dirs.append(path)

    owners = []
    if getattr(reticulum, "rpc_type", None) == "AF_UNIX":
        owners = _socket_owners(f"rns/{reticulum.local_socket_path or 'default'}")
    for pid in owners:
        process = _process_args(pid)
        if process is not None:
            add(dirs_in_args(*process))
    rnfs_dir_value = rnfs_reticulum_dir(rnfs_dir)
    add([rnfs_dir_value] if rnfs_dir_value else [])
    add(DEFAULT_DIRS)
    if not owners:  # couldn't tell which process it is (TCP instance, or not Linux): any of this user's
        for pid in _pids():
            process = _process_args(pid)
            if process is not None:
                add(dirs_in_args(*process))
    return dirs


def match_shared_instance(reticulum, rnfs_dir: str) -> str | None:
    """If attached to a shared instance that refuses this process's RPC key, find and use the right key.

    Returns the config directory the key came from, or None if nothing needed changing. Raises LookupError if
    the instance refuses every key found."""
    if not reticulum.is_connected_to_shared_instance or rpc_accepts(reticulum, reticulum.rpc_key):
        return None
    tried = {reticulum.rpc_key}
    for directory in candidate_dirs(reticulum, rnfs_dir):
        for key in keys_in(directory):
            if key in tried or len(tried) > MAX_TRIES:
                continue
            tried.add(key)
            if rpc_accepts(reticulum, key):
                reticulum.rpc_key = key  # RNS reads it for each RPC call
                return directory
    raise LookupError("no key the shared instance accepts was found")
