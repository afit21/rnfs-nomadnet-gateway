"""`rnfs-nomadnet-gateway`: run the gateway until SIGINT or SIGTERM."""
import argparse
import os
import signal
import sys
import threading

import rnfs  # light: rnfs.Client never loads RNS

from rnfs_nomadnet_gateway import __version__
from rnfs_nomadnet_gateway.config import DEFAULT_GATEWAY_DIR, Config, write_example

CHECK_TIMEOUT = 30.0  # seconds --check waits for the daemon


def fail(message: str):
    sys.exit(f"rnfs-nomadnet-gateway: {message}")


def private_dir(path: str):
    """Make path (owner-only) if needed; refuse one that belongs to another user, as rnfs does."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "nt":
        return
    info = os.stat(path)
    if info.st_uid != os.geteuid():
        fail(f"{path} belongs to another user (uid {info.st_uid}); the gateway dir must be your own")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="rnfs-nomadnet-gateway",
                                     description="Serve rnfs content to NomadNet clients as a NomadNet node.")
    parser.add_argument("--dir", default=DEFAULT_GATEWAY_DIR,
                        help=f"gateway dir: config.toml, identity and spool (default {DEFAULT_GATEWAY_DIR})")
    parser.add_argument("--rnsconfig", metavar="DIR", help="Reticulum config dir (overrides reticulum_dir)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more log output (repeatable)")
    parser.add_argument("--print-address", action="store_true",
                        help="print the gateway's node address and exit, without starting Reticulum")
    parser.add_argument("--check", action="store_true",
                        help="check the settings and that the rnfs daemon's API answers, then exit, without starting "
                             "Reticulum")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # RNS logs with print(): under systemd stdout is a pipe, block-buffered, and lines would only reach the
    # journal when the buffer fills or the process exits
    sys.stdout.reconfigure(line_buffering=True)
    gateway_dir = os.path.expanduser(args.dir)
    private_dir(gateway_dir)
    write_example(gateway_dir)
    try:
        config = Config.load(args.dir, reticulum_dir=args.rnsconfig)
    except (ValueError, TypeError) as e:
        fail(f"invalid settings: {e}")
    try:  # no timeout but for --check: a fetch can take hours
        api = rnfs.Client(config.rnfs_dir, port=config.api_port, timeout=CHECK_TIMEOUT if args.check else None)
    except rnfs.ApiError as e:
        fail(f"invalid settings: {e}")
    if args.check:
        try:
            node_id = api.node_id.hex()
        except rnfs.ApiError as e:
            fail(f"can't use the rnfs daemon: {e}")
        print(f"OK: the rnfs daemon answers, as node {node_id} (rnfs dir {config.rnfs_dir})")
        return

    # Imported here so --help, --check and settings errors don't pay for loading RNS
    import RNS

    from rnfs_nomadnet_gateway.gateway import Gateway, log
    from rnfs_nomadnet_gateway.node import IDENTITY_FILE, GatewayNode, destination_hash, load_identity
    from rnfs_nomadnet_gateway.pins import PINS_FILE, PinLedger
    from rnfs_nomadnet_gateway.rns_instance import match_shared_instance
    from rnfs_nomadnet_gateway.spool import Spool

    try:
        identity = load_identity(config.path(IDENTITY_FILE))
    except (OSError, ValueError) as e:
        fail(f"can't load the gateway's identity: {e}")
    if args.print_address:
        print(destination_hash(identity).hex())
        return

    reticulum_dir = os.path.expanduser(config.reticulum_dir) if config.reticulum_dir else None
    # Attaches to a shared instance (rnsd, or e.g. the rnfs daemon) if one is running, like any RNS program
    reticulum = RNS.Reticulum(configdir=reticulum_dir, verbosity=args.verbose or None)
    try:
        source = match_shared_instance(reticulum, config.rnfs_dir)
    except LookupError:
        log("attached to a shared Reticulum instance that refuses this process's RPC key, and none it accepts was "
            "found: identifying visitors will fail. Set reticulum_dir to the config directory the shared instance "
            "was started with, or the same rpc_key in both configs", RNS.LOG_ERROR)
    else:
        if source is not None:
            log(f"attached to the shared Reticulum instance with the RPC key of {source}", RNS.LOG_NOTICE)
    spool = Spool(config.path("spool"), config.spool_max, config.spool_ttl)
    pins = PinLedger(config.path(PINS_FILE), config.auto_pin_quota, api, log=lambda message: log(message))
    gateway = Gateway(config, api, spool, pins=pins)
    node = GatewayNode(config, gateway, identity)

    if config.access == "whitelist" and not config.allowed_identities:
        log(f"access is \"whitelist\" and allowed_identities is empty, so nobody can use the gateway; add "
            f"identity hashes to {config.path('config.toml')}", RNS.LOG_WARNING)
    elif config.access == "open":
        log("access is \"open\": anyone can use the gateway", RNS.LOG_NOTICE)
    spool.clean()
    node.start()
    print(f"rnfs NomadNet gateway {RNS.prettyhexrep(node.hash)} ({config.node_name}), using the rnfs daemon of "
          f"{config.rnfs_dir}", flush=True)

    # Replace RNS's handlers, which exit at once, so the gateway stops first
    stopping = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stopping.set())
    while not stopping.wait(1):
        pass
    node.stop()
    gateway.close()
    api.close()
    sys.stdout.flush()
    sys.stderr.flush()
    RNS.exit()
