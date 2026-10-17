import unittest

from rnfs_nomadnet_gateway.targets import Target, parse_target
from tests.support import cid

FILE = cid("file")
DIR = cid("dir", dag=True)


class ParseTest(unittest.TestCase):
    def test_bare_and_paths(self):
        self.assertEqual(parse_target(FILE), Target(FILE))
        self.assertEqual(parse_target(f"  {DIR}/docs/report.pdf \n"), Target(DIR, ("docs", "report.pdf")))
        self.assertEqual(parse_target(f"{DIR}/docs/"), Target(DIR, ("docs",)))

    def test_refused(self):
        for text in [None, "", "  ", "alice/docs", "name/" + "a" * 32, f"{DIR}//x", f"{DIR}/..", f"{DIR}/.",
                     f"{DIR}/a\0b", f"{DIR}/" + "x" * 256, f"{DIR}/" + "/".join("a" * 65),
                     "rnfs:name/" + "a" * 32, f"{DIR}/caf%C3%A9"]:  # %: only in an rnfs: link, as rnfs takes it
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_target(text)

    def test_uri(self):
        self.assertEqual(parse_target(f"rnfs:{DIR}/photos/caf%C3%A9%20night.jpg?from={'9e' * 16}"),
                         Target(DIR, ("photos", "café night.jpg")))
        self.assertEqual(parse_target(f"RNFS:{DIR}/a/"), Target(DIR, ("a",)))
        self.assertEqual(parse_target(f"rnfs:{FILE}?x=1"), Target(FILE))
        for text in [f"rnfs:{DIR}/a b", f"rnfs:{DIR}#x", f"rnfs:{DIR}/%FF", f"rnfs:{DIR}/%2F",
                     f"rnfs:{DIR}?from=1&from=2", f"rnfs:{DIR}?from=zz", f"rnfs:{DIR}?", f"rnfs:{DIR}//a",
                     f"rnfs:{DIR}/é", "rnfs:" + "x" * 5000]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_target(text)

    def test_round_trip(self):
        target = Target(DIR, ("a|b=c", "d`e]f", "g h", "ünï"))
        self.assertEqual(parse_target(target.uri()), target)
        value = target.link_value()
        for c in "`|=] ":
            self.assertNotIn(c, value)
        self.assertEqual(Target(FILE).link_value(), FILE)
        self.assertEqual(target.key, f"{DIR}/a|b=c/d`e]f/g h/ünï")

    def test_child_parent(self):
        target = Target(DIR).child("a")
        self.assertEqual(target.parent, Target(DIR))
        self.assertIsNone(Target(DIR).parent)
        with self.assertRaises(ValueError):
            Target(DIR).child("a/b")


if __name__ == "__main__":
    unittest.main()
