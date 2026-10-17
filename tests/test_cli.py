import os
import tempfile
import unittest

from rnfs_nomadnet_gateway import cli


class CheckTest(unittest.TestCase):
    def test_check_without_daemon(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "rnfs"))
            with open(os.path.join(d, "config.toml"), "w") as f:
                f.write(f'rnfs_dir = "{d}/rnfs"\napi_port = 1\n')
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--dir", d, "--check"])
            self.assertIn("can't use the rnfs daemon: no API token", str(raised.exception.code))
            self.assertFalse(os.path.exists(os.path.join(d, "identity")))  # checked before anything is made


if __name__ == "__main__":
    unittest.main()
