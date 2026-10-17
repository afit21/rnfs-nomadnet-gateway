import os
import tempfile
import time
import unittest

from rnfs_nomadnet_gateway import micron, webpage
from rnfs_nomadnet_gateway.config import EXAMPLE, Config, write_example


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def test_defaults_and_example(self):
        write_example(self.dir.name)
        config = Config.load(self.dir.name)
        self.assertEqual(config.max_file_size, 100 * 1024 * 1024)
        self.assertEqual(config.access, "whitelist")
        # Every commented setting in the example is a real one, with its default value
        for line in EXAMPLE.splitlines():
            if line.startswith("# ") and " = " in line:
                name, value = line[2:].split("#")[0].split(" = ")
                if name.strip() not in ("api_port", "reticulum_dir", "rnfs_dir"):
                    self.write("config.toml", f"{name.strip()} = {value.strip()}\n")
                    self.assertEqual(Config.load(self.dir.name), config, name)

    def test_settings(self):
        self.write("config.toml", 'access = "open"\nallowed_identities = ["<' + "ab" * 16 + '>"]\n'
                                  "failure_ttl = 60\n")
        config = Config.load(self.dir.name)
        self.assertEqual(config.allowed, {bytes.fromhex("ab" * 16)})
        for text in ['access = "closed"', "max_workers = 0", "max_fetches = true", 'allowed_identities = ["x"]',
                     "failure_ttl = 0", "bogus = 1", "spool_max = 1.5", "node_name = ''", "html_pages = 1", "view_page_lines = 0",
                     "base64_max_size = -1"]:
            with self.subTest(text=text):
                self.write("config.toml", text + "\n")
                with self.assertRaises(ValueError):
                    Config.load(self.dir.name)


class MicronTest(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(micron.escape("a`b\\c\x1b[31m\nd‮"), "a\\`b\\\\c[31md")

    def test_link(self):
        self.assertEqual(micron.link("Go`]", "/page/get.mu", target="x"), "`[Go')`:/page/get.mu`target=x]")
        for bad in ["a|b", "a=b", "a`b", "a]b", "a\nb"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                micron.link("x", "/page/get.mu", target=bad)

    def test_hosted_micron(self):
        text = "#!c=0\n#!bg=000\n>Title\n`{abc:/page/p.mu}\n`(img`:/media/x.png)\nok \x1b[31mred\n`=\n`{kept}\n"
        self.assertEqual(micron.hosted_micron(text), ">Title\nok [31mred\n`=\n`{kept}\n`=")
        self.assertEqual(micron.hosted_micron("a\n`=\nb\n`=\nc"), "a\n`=\nb\n`=\nc")

    def test_hosted_micron_prefixed_partials(self):
        # NomadNet reads these as partials too, after a section reset, an escape, or a heading holding a field
        partial = "`{abc:/page/p.mu`5}"
        for line in ["<" + partial, "<<" + partial, "\\" + partial, ">" + partial + "`<f`x>", "<\\#!bg=000"]:
            with self.subTest(line=line):
                self.assertEqual(micron.hosted_micron(f"a\n{line}\nb"), "a\nb")

    def test_hosted_micron_closes_tables(self):
        self.assertEqual(micron.hosted_micron("a\n`t\n|x|"), "a\n`t\n|x|\n`t")
        self.assertEqual(micron.hosted_micron("<`tc\n|x|\n\\`t"), "`tc\n|x|\n`t")  # written plainly
        self.assertEqual(micron.hosted_micron("`t\n`=\n`t\n`="), "`t\n`=\n`t\n`=\n`t")  # in a literal: text

    def test_literal_text(self):
        self.assertEqual(micron.literal_text("a`b\n`=\nc\x07"), "`=\na`b\n\\`=\nc\n`=")

    def test_to_html(self):
        page = webpage.to_html("#!c=0\n>T<i>\n`F4aeblue`f `Fzzzbad\n`[Home`:/page/index.mu]\n"
                               "`[web`https://example.com]\n`[x`:/page/get.mu`a=1|b=2]\n`=\n<b>`!\n`=\n-").decode()
        self.assertIn("<title>T&lt;i&gt;</title>", page)
        self.assertIn("<h1>T&lt;i&gt;</h1>", page)  # every piece of text escaped
        self.assertIn('<span style="color:#4ae">blue</span> bad', page)  # a colour must be 3 hex digits
        self.assertIn('<a href=":/page/index.html">Home</a>', page)  # the gateway's pages stay HTML
        self.assertNotIn("example.com\"", page)  # only node links are kept
        self.assertIn('<a href=":/page/get.html`a=1|b=2">x</a>', page)
        self.assertIn("<pre>&lt;b&gt;`!</pre>", page)
        self.assertIn("<hr>", page)
        self.assertNotIn("c=0", page)

    def test_to_html_is_linear(self):
        start = time.monotonic()
        webpage.to_html("`<`>" * 512 * 1024)  # 2 MiB of fields on one line: took 8 s when quadratic
        self.assertLess(time.monotonic() - start, 3)

    def test_names_cant_set_page_colours(self):
        page = micron.page("x", "  Name x#!bg=fff").decode()  # NomadNet uses the first #!bg= anywhere
        self.assertTrue(page.startswith("#!c=0\n#!bg=\n#!fg=\n"))

    def test_to_html_drops_forms(self):
        page = webpage.to_html(">>Open\nType here:\n`<24|target`>`[Go`:/page/get.mu`target]\n\nkept").decode()
        self.assertIn("<h2>Open</h2>", page)
        self.assertNotIn("Type here", page)
        self.assertNotIn("Go", page)
        self.assertIn("kept", page)

    def test_size(self):
        self.assertEqual(micron.size(5), "5 bytes")
        self.assertEqual(micron.size(1536), "1.5 KiB")
        self.assertEqual(micron.size(100 * 1024 * 1024), "100.0 MiB")


if __name__ == "__main__":
    unittest.main()
