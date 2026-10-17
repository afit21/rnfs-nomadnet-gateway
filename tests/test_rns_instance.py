import multiprocessing.connection
import os
import tempfile
import threading
import types
import unittest

import RNS

from rnfs_nomadnet_gateway import rns_instance
from rnfs_nomadnet_gateway.rns_instance import dirs_in_args, keys_in, match_shared_instance, rpc_accepts


def make_config_dir(root: str, name: str, rpc_key: str | None = None) -> tuple[str, bytes]:
    """A Reticulum config dir with a transport identity; its RPC key."""
    directory = os.path.join(root, name)
    os.makedirs(os.path.join(directory, "storage"))
    identity = RNS.Identity()
    with open(os.path.join(directory, "storage", "transport_identity"), "wb") as f:
        f.write(identity.get_private_key())
    with open(os.path.join(directory, "config"), "w") as f:
        f.write("[reticulum]\n" + (f"  rpc_key = {rpc_key}\n" if rpc_key else ""))
    key = bytes.fromhex(rpc_key) if rpc_key else RNS.Identity.full_hash(identity.get_private_key())
    return directory, key


class FakeInstance:
    """An RPC listener like a shared instance's, answering one call per connection."""

    def __init__(self, key: bytes):
        self.listener = multiprocessing.connection.Listener(("127.0.0.1", 0), family="AF_INET", authkey=key)
        self.stopped = False
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stopped:
            try:
                connection = self.listener.accept()
                connection.recv_bytes()
                connection.send_bytes(RNS.vendor.umsgpack.packb(False))
                connection.close()
            except Exception:  # noqa: BLE001 - a refused key, as RNS logs and carries on
                continue

    def reticulum(self, key: bytes):
        return types.SimpleNamespace(rpc_addr=self.listener.address, rpc_type="AF_INET", rpc_key=key,
                                     is_connected_to_shared_instance=True, local_socket_path=None)


class RnsInstanceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_keys_in(self):
        directory, key = make_config_dir(self.dir.name, "a")
        self.assertEqual(keys_in(directory), [key])
        directory, key = make_config_dir(self.dir.name, "b", rpc_key="ab" * 32)
        self.assertEqual(keys_in(directory)[0], key)
        self.assertEqual(keys_in(os.path.join(self.dir.name, "missing")), [])

    def test_dirs_in_args(self):
        a, _ = make_config_dir(self.dir.name, "a")
        b, _ = make_config_dir(self.dir.name, "b")
        args = ["python3", "/usr/bin/rnfs", "daemon", "--rnsconfig", "a", f"--config={b}", "--dir", self.dir.name]
        self.assertEqual(dirs_in_args(args, self.dir.name), [a, b])

    def test_rpc_accepts(self):
        instance = FakeInstance(b"k" * 32)
        self.assertTrue(rpc_accepts(instance.reticulum(None), b"k" * 32))
        self.assertFalse(rpc_accepts(instance.reticulum(None), b"x" * 32))

    def test_match(self):
        right, key = make_config_dir(self.dir.name, "right")
        wrong, wrong_key = make_config_dir(self.dir.name, "wrong")
        instance = FakeInstance(key)
        reticulum = instance.reticulum(wrong_key)
        original = rns_instance.candidate_dirs
        rns_instance.candidate_dirs = lambda r, rnfs_dir: [wrong, right]
        self.addCleanup(setattr, rns_instance, "candidate_dirs", original)
        self.assertEqual(match_shared_instance(reticulum, "~/.rnfs"), right)
        self.assertEqual(reticulum.rpc_key, key)
        self.assertIsNone(match_shared_instance(reticulum, "~/.rnfs"))  # already right
        reticulum.rpc_key = wrong_key
        rns_instance.candidate_dirs = lambda r, rnfs_dir: [wrong]
        with self.assertRaises(LookupError):
            match_shared_instance(reticulum, "~/.rnfs")

    def test_not_attached(self):
        reticulum = types.SimpleNamespace(is_connected_to_shared_instance=False)
        self.assertIsNone(match_shared_instance(reticulum, "~/.rnfs"))


if __name__ == "__main__":
    unittest.main()
