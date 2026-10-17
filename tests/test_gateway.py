import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import rnfs
from rnfs import ApiError

from rnfs_nomadnet_gateway.config import Config
from rnfs_nomadnet_gateway.gateway import Gateway
from rnfs_nomadnet_gateway.micron import FILE_PATH, GET_HTML, GET_PAGE, INDEX_HTML, INDEX_PAGE
from rnfs_nomadnet_gateway.spool import Spool
from tests.support import FakeClock, cid, wait_for

ROOT = cid("root", dag=True)
SUB = cid("sub", dag=True)
REPORT = cid("report")
BIG = cid("big")
SEALED = cid("sealed")
ALICE = bytes.fromhex("aa" * 16)
BOB = bytes.fromhex("bb" * 16)


class FakeApi:
    """Answers like rnfs.Client from a table; `gate` holds calls until set, to see pages while they run."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []
        self.sources: dict[str, set] = {}  # CID → pin sources
        self.fail_get: ApiError | None = None
        self.contents: dict[str, bytes] = {}  # CID → what a fetch writes (else x's)
        self.info_table = {
            ROOT: {"cid": ROOT, "type": "directory", "size": 105, "executable": False},
            f"{ROOT}/docs": {"cid": SUB, "type": "directory", "size": 5, "executable": False},
            f"{ROOT}/docs/report.pdf": {"cid": REPORT, "type": "file", "size": 5, "executable": False},
            f"{ROOT}/.hidden": {"cid": REPORT, "type": "file", "size": 5, "executable": False},
            REPORT: {"cid": REPORT, "type": "file", "size": 5, "executable": False},
            BIG: {"cid": BIG, "type": "file", "size": 10 ** 9, "executable": False},
            SEALED: {"cid": SEALED, "type": "file", "size": 3, "executable": False, "sealed": True,
                     "recipient": True, "stored_size": 200, "name": "secret.txt"},
        }
        self.listings = {
            ROOT: [{"name": "docs", "kind": "directory", "size": 5, "cid": SUB},
                   {"name": "a`b]c", "kind": "executable", "size": 100, "cid": REPORT}],
            f"{ROOT}/docs": [{"name": "report.pdf", "kind": "file", "size": 5, "cid": REPORT}],
        }

    def info(self, target):
        self.calls.append(("info", target))
        self.gate.wait()
        if target not in self.info_table:
            raise ApiError("no providers found for it", 404)
        return rnfs.Info.from_dict(self.info_table[target])

    def ls(self, target):
        self.calls.append(("ls", target))
        self.gate.wait()
        return [rnfs.Entry.from_dict(entry) for entry in self.listings[target]]

    def pin(self, target):
        self.calls.append(("pin", target))
        self.gate.wait()
        self.sources.setdefault(self.info_table[target]["cid"], set()).add("user")

    def pins(self, cid):
        return [rnfs.Pin.from_dict({"cid": cid, "sources": sorted(self.sources[cid])})] if self.sources.get(cid) else []

    def unpin(self, cid):
        self.calls.append(("unpin", cid))
        if "user" not in self.sources.get(cid, ()):
            raise rnfs.NotFound(f"{cid} is not pinned by the user")
        self.sources[cid].discard("user")

    def get(self, target, output, raw=False):
        assert raw, "the gateway always fetches raw"
        self.calls.append(("get", target, output))
        self.gate.wait()
        if self.fail_get is not None:
            raise self.fail_get
        partial = os.path.join(os.path.dirname(output), ".rnfs-get-x.partial")
        info = self.info_table[target]
        with open(partial, "wb") as f:
            f.write(self.contents.get(info["cid"], b"x" * info.get("stored_size", info["size"])))
        os.rename(partial, output)
        return {"cid": self.info_table[target]["cid"], "warnings": []}


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = FakeClock()
        self.api = FakeApi()
        self.make(access="whitelist", allowed_identities=[ALICE.hex(), f"<{BOB.hex()}>"])

    def make(self, **settings):
        settings = {"auto_pin_max_size": 0, "page_wait": 0, **settings}
        self.config = Config(gateway_dir=self.dir.name, rnfs_dir=self.dir.name, **settings)
        self.config.validate()
        self.spool = Spool(self.config.path("spool"), self.config.spool_max, self.config.spool_ttl)
        self.gateway = Gateway(self.config, self.api, self.spool, clock=self.clock)
        self.addCleanup(self.gateway.close)

    def idle(self):
        wait_for(lambda: self.gateway.pending == 0)

    def page(self, path, identity=ALICE, **variables) -> str:
        response = self.gateway.respond(path, {f"var_{k}": v for k, v in variables.items()}, identity, owner="link")
        self.assertIsInstance(response, bytes)
        text = response.decode("utf-8")
        self.assertTrue(text.startswith("#!c=0\n"))
        return text

    def settle(self, identity=ALICE, **variables) -> str:
        """The get page once its tasks have finished."""
        self.page(GET_PAGE, identity, **variables)
        self.idle()
        variables.pop("action", None)
        return self.page(GET_PAGE, identity, **variables)

    # Access

    def test_whitelist(self):
        page = self.page(INDEX_PAGE, identity=None)
        self.assertIn("Identify When Connecting", page)
        stranger = bytes.fromhex("cc" * 16)
        page = self.page(GET_PAGE, identity=stranger, target=REPORT)
        self.assertIn("cc" * 16, page)
        self.assertEqual(self.api.calls, [])  # refused before anything reaches the API
        self.assertIn("Open a file", self.page(INDEX_PAGE, identity=BOB))

    def test_open(self):
        self.make(access="open")
        page = self.page(INDEX_PAGE, identity=None)
        self.assertIn("share limits", page)
        self.assertNotIn(">>Recent", page)  # anonymous visitors' fetches aren't shown to each other

    # Lookups

    def test_invalid_target_never_reaches_api(self):
        for value in ["alice/docs", "name/" + "a" * 32, "hello", ""]:
            page = self.page(GET_PAGE, target=value)
            self.assertIn("Can't open that", page)
        self.assertEqual(self.api.calls, [])

    def test_form_field_preferred(self):
        response = self.gateway.respond(GET_PAGE, {"field_target": f" rnfs:{REPORT} ", "var_target": "x"}, ALICE)
        self.assertIn(b"Looking up", response)

    def test_other_clients_request_data(self):
        for data in ({b"field_target": REPORT.encode()}, {"target": REPORT}, {"field_target": REPORT.encode()}):
            with self.subTest(data=data):
                self.assertIn(b"Looking up", self.gateway.respond(GET_PAGE, data, ALICE))
                self.idle()
                self.gateway.entries.clear()

    def test_lookup_then_file(self):
        self.api.gate.clear()
        page = self.page(GET_PAGE, target=f"{ROOT}/docs/report.pdf")
        self.assertIn("Looking up", page)
        self.assertIn("Refresh", page)
        self.api.gate.set()
        self.idle()
        page = self.page(GET_PAGE, target=f"{ROOT}/docs/report.pdf")
        self.assertIn("Name     `freport.pdf", page)
        self.assertIn(REPORT, page)
        self.assertIn("`[Fetch`", page)
        self.assertIn("action=fetch", page)
        self.assertIn(f"`[↑ Folder`:/page/get.mu`target=rnfs:{ROOT}/docs]", page)
        self.assertEqual(self.api.calls, [("info", f"{ROOT}/docs/report.pdf")])  # nothing fetched unasked

    def test_lookup_failure_remembered(self):
        missing = cid("missing")
        page = self.settle(target=missing)
        self.assertIn("Lookup failed", page)
        self.assertIn("no providers found", page)
        self.assertIn("action=retry", page)
        self.page(GET_PAGE, target=missing)
        self.assertEqual(len(self.api.calls), 1)  # refreshing doesn't repeat it
        self.clock.now += self.config.failure_ttl
        self.page(GET_PAGE, target=missing)
        self.idle()
        self.assertEqual(len(self.api.calls), 2)  # forgotten after failure_ttl
        self.page(GET_PAGE, target=missing, action="retry")
        self.idle()
        self.assertEqual(len(self.api.calls), 3)  # or retried at once

    def test_local_paths_hidden(self):
        self.api.info_table.pop(REPORT)
        original = self.api.info

        def info(target):
            raise ApiError(f"can't write {self.dir.name}/spool/x: no", 400)
        self.api.info = info
        page = self.settle(target=REPORT)
        self.assertNotIn(self.dir.name, page)
        self.api.info = original

    def test_daemon_down(self):
        def info(target):
            raise ApiError("can't reach the rnfs daemon on API port 8788: refused /home/x")
        self.api.info = info
        page = self.settle(target=REPORT)
        self.assertIn("can't reach rnfs", page)
        self.assertNotIn("/home/x", page)

    def test_front_page(self):
        page = self.page(INDEX_PAGE)
        self.assertIn("Get files from `!rnfs`!", page)
        self.assertIn("About rnfs: `F4aehttps://github.com/afit21/rnfs/`f", page)
        self.assertNotIn("`[https://", page)  # not a link: NomadNet can't open web addresses
        self.assertNotIn("|target`", page)  # no form: clients send them differently, so it's open by address
        self.assertIn("`=\n  <gateway address>:/page/index.mu`cid=<CID>\n`=", page)
        self.assertIn("`[HTML version`:/page/index.html]", page)
        self.gateway.address = "ab" * 16
        self.assertIn(f"`=\n  {'ab' * 16}:/page/index.mu`cid=<CID>\n`=", self.page(INDEX_PAGE))

    def test_index_with_cid(self):
        self.gateway.respond(INDEX_PAGE, {"var_cid": REPORT}, ALICE)
        self.idle()
        page = self.page(INDEX_PAGE, cid=REPORT)
        self.assertIn("`[Fetch`", page)
        self.assertEqual(self.api.calls, [("info", REPORT)])

    def test_output_name(self):
        variables = {"var_cid": REPORT, "var_o": "manifesto.pdf"}
        self.gateway.respond(INDEX_PAGE, variables, ALICE)
        self.idle()
        page = self.gateway.respond(INDEX_PAGE, variables, ALICE).decode()
        self.assertIn("Saves as `fmanifesto.pdf", page)
        self.assertIn(f"`[Fetch`:/page/get.mu`target={REPORT}|action=fetch|o=manifesto.pdf]", page)
        self.gateway.respond(GET_PAGE, {"var_target": REPORT, "var_action": "fetch", "var_o": "manifesto.pdf"}, ALICE)
        self.idle()
        page = self.page(GET_PAGE, target=REPORT, o="manifesto.pdf")
        self.assertIn(f"`[Download manifesto.pdf`:/file/{REPORT}/manifesto.pdf]", page)
        response = self.gateway.respond(FILE_PATH, {"var_target": REPORT, "var_o": "manifesto.pdf"}, ALICE, "link")
        self.assertEqual(response[1], {"name": b"manifesto.pdf"})
        response[0].close()
        self.assertIn("manifesto.pdf", self.page(INDEX_PAGE))  # recent fetches keep the name

    def test_unfit_output_name(self):
        for name in [".bashrc", "-rf", "a/b", "a\\b", "x" * 300]:
            with self.subTest(name=name):
                self.gateway.respond(INDEX_PAGE, {"var_cid": REPORT, "var_o": name}, ALICE)
                self.idle()
                page = self.gateway.respond(INDEX_PAGE, {"var_cid": REPORT, "var_o": name}, ALICE).decode()
                self.assertIn(f"Saves as `f{REPORT}", page)
                self.assertIn("Can't use the name", page)
                self.assertNotIn("|o=", page)

    def test_small_files_fetched_and_pinned(self):
        self.make(auto_pin_max_size=10, allowed_identities=[ALICE.hex()])
        self.page(GET_PAGE, target=REPORT)  # 5 bytes
        self.idle()
        self.page(GET_PAGE, target=REPORT)  # the lookup is done: the fetch starts without being asked
        self.idle()
        self.assertEqual([c[0] for c in self.api.calls], ["info", "pin", "get"])
        self.assertIn("Download", self.page(GET_PAGE, target=REPORT))
        self.assertEqual(self.gateway.limits.remaining(ALICE.hex()), self.config.fetches_per_hour - 1)
        page = self.settle(target=BIG)  # too large to fetch unasked
        self.assertIn("Too big", page)
        self.assertNotIn(("pin", BIG), self.api.calls)

    def test_larger_files_not_pinned(self):
        self.make(auto_pin_max_size=4, allowed_identities=[ALICE.hex()])
        page = self.settle(target=REPORT)
        self.assertIn("`[Fetch`", page)
        self.page(GET_PAGE, target=REPORT, action="fetch")
        self.idle()
        self.assertEqual([c[0] for c in self.api.calls], ["info", "get"])

    def test_page_waits_for_quick_work(self):
        self.make(auto_pin_max_size=10, page_wait=5, allowed_identities=[ALICE.hex()])
        page = self.page(GET_PAGE, target=REPORT)  # lookup, fetch and pin, all within page_wait
        self.assertIn(f"`[Download {REPORT}`:/file/{REPORT}/{REPORT}]", page)

    def test_page_wait_is_bounded(self):
        self.make(page_wait=0.2, allowed_identities=[ALICE.hex()])
        self.api.gate.clear()
        page = self.page(GET_PAGE, target=REPORT)
        self.assertIn("Looking up", page)
        self.api.gate.set()

    def test_operators_own_pin_is_left_alone(self):
        self.api.sources[REPORT] = {"user", "folder"}
        self.make(auto_pin_max_size=10, allowed_identities=[ALICE.hex()])
        self.settle(target=REPORT)
        self.idle()
        self.assertNotIn(("pin", REPORT), self.api.calls)
        self.assertEqual(self.gateway.pins.pins, {})  # not recorded, so the quota never releases it

    def test_view_text_and_micron(self):
        self.api.info_table[f"{ROOT}/page.mu"] = {"cid": REPORT, "type": "file", "size": 5, "executable": False}
        self.make(auto_pin_max_size=10, view_max_size=10, allowed_identities=[ALICE.hex()])
        self.settle(target=f"{ROOT}/page.mu")
        self.idle()
        page = self.page(GET_PAGE, target=f"{ROOT}/page.mu")
        self.assertIn(f"`[View`:/page/get.mu`target=rnfs:{ROOT}/page.mu|action=view]", page)
        page = self.page(GET_PAGE, target=f"{ROOT}/page.mu", action="view")
        self.assertIn("Showing page.mu", page)
        self.assertTrue(page.startswith("#!c=0\n#!bg=\n#!fg=\n"))  # hosted text can't set the page's colours
        self.assertIn("\n-\nxxxxx\n``\n<\n", page)  # Micron: the content as markup
        self.settle(target=REPORT)
        page = self.page(GET_PAGE, target=REPORT, action="view")  # no .mu name: shown as written
        self.assertIn("\n-\n`=\nxxxxx\n`=\n", page)

    def test_view_refused(self):
        self.make(auto_pin_max_size=10, view_max_size=4, allowed_identities=[ALICE.hex()])
        self.settle(target=REPORT)
        self.idle()
        page = self.page(GET_PAGE, target=REPORT, action="view")  # 5 bytes, over view_max_size
        self.assertIn("Can't show this one", page)
        self.assertNotIn("`[View", page)

    def test_view_without_refresh(self):
        self.make(auto_pin_max_size=10, view_max_size=10, page_wait=5, allowed_identities=[ALICE.hex()])
        page = self.page(INDEX_PAGE, cid=REPORT, o="hello.mu", action="view")  # lookup, fetch, then the view
        self.assertIn("Showing hello.mu", page)

    # Pages as HTML, print=1 and base64=1

    def hosted(self, name: str, content: bytes) -> str:
        """A file named name in ROOT, holding content; its target."""
        c = cid(name)
        target = f"{ROOT}/{name}"
        self.api.info_table[target] = {"cid": c, "type": "file", "size": len(content), "executable": False}
        self.api.contents[c] = content
        return target

    def respond(self, path, **variables) -> bytes:
        return self.gateway.respond(path, {f"var_{k}": v for k, v in variables.items()}, ALICE, "link")

    def test_html_pages(self):
        page = self.respond(INDEX_HTML).decode()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("https://github.com/afit21/rnfs/", page)
        self.assertIn('<a href=":/page/index.html">Home</a>', page)  # links stay on the HTML pages
        self.assertIn("&lt;gateway address&gt;:/page/index.mu`cid=&lt;CID&gt;", page)
        self.assertEqual(self.respond(INDEX_PAGE, html="1")[:15], b"<!doctype html>")  # or html=1
        self.settle(target=f"{ROOT}/docs/report.pdf")
        page = self.respond(GET_HTML, target=f"{ROOT}/docs/report.pdf").decode()
        self.assertIn(f'href=":/page/get.html`target=rnfs:{ROOT}/docs"', page)  # ↑ Folder
        self.assertIn("report.pdf", page)

    def test_html_pages_off(self):
        self.make(html_pages=False, allowed_identities=[ALICE.hex()])
        page = self.page(INDEX_PAGE, html="1")  # still Micron
        self.assertNotIn("HTML version", page)

    def test_print(self):
        self.make(auto_pin_max_size=1000, page_wait=5, allowed_identities=[ALICE.hex()])
        site = self.hosted("site.mu", b"#!bg=222\n>Hi\n<`{abc:/page/p.mu}\ntext")
        self.assertEqual(self.respond(GET_PAGE, target=site, print="1"), b"#!bg=222\n>Hi\ntext")  # as its own page
        self.assertIn(b"<h1>Hi</h1>", self.respond(GET_HTML, target=site, print="1"))  # asked for as HTML
        notes = self.hosted("notes.txt", b"a `!b`!")
        self.assertEqual(self.respond(GET_PAGE, target=notes, print="1"), b"`=\na `!b`!\n`=\n")  # as written
        web = self.hosted("index.html", b"<p>Hi</p>\n<`{abc:/page/p.mu}")
        self.assertEqual(self.respond(GET_HTML, target=web, print="1"), b"<p>Hi</p>")
        page = self.page(GET_PAGE, target=web)  # its View link opens it as HTML
        self.assertIn(f"`[View`:/page/get.html`target=rnfs:{web}|print=1]", page)
        binary = self.hosted("photo.jpg", b"\xff\xd8\x00")
        self.assertIn(b"Can't show this one", self.respond(GET_PAGE, target=binary, print="1"))

    def test_print_fetches_the_file(self):
        self.make(view_max_size=2 * 1024 * 1024, allowed_identities=[ALICE.hex()])  # auto_pin_max_size 0
        book = self.hosted("book.txt", b"\xef\xbb\xbfCall me Ishmael.\r\n" * 70_000)  # 1.3 MB, CRLF, a BOM
        page = self.respond(GET_PAGE, target=book, print="1").decode()
        self.assertIn(f"`[Refresh`:/page/get.mu`target=rnfs:{book}|print=1]", page)  # looking up
        self.idle()
        page = self.respond(GET_PAGE, target=book, print="1").decode()  # asking to see it fetches it
        self.assertIn("Fetching", page)
        self.assertIn(f"`[Refresh`:/page/get.mu`target=rnfs:{book}|print=1]", page)
        self.idle()
        text = self.respond(GET_PAGE, target=book, print="1")
        self.assertTrue(text.startswith(b"`=\nCall me Ishmael.\nCall me Ishmael.\n"))
        self.assertIn(("pin", book), self.api.calls)  # over auto_pin_max_size, but inside a folder: pinned
        notes = self.hosted("notes.md", b"# Notes")
        self.respond(GET_PAGE, target=notes, action="view")
        self.idle()
        page = self.respond(GET_PAGE, target=notes, action="view").decode()
        self.assertIn(f"`[Refresh`:/page/get.mu`target=rnfs:{notes}|action=view]", page)
        self.idle()
        self.assertIn("Showing notes.md", self.page(GET_PAGE, target=notes, action="view"))

    def test_text_paged(self):
        self.make(auto_pin_max_size=10_000, page_wait=5, view_page_lines=50, allowed_identities=[ALICE.hex()])
        book = self.hosted("book.txt", "\n".join(f"line {i}" for i in range(1, 121)).encode())
        first = self.respond(GET_PAGE, target=book, print="1").decode()
        self.assertTrue(first.startswith("`=\nline 1\n"))
        self.assertIn("\nline 50\n`=\n", first)
        self.assertIn("Lines 1-50 of 120", first)
        self.assertIn(f"`[Next →`:/page/get.mu`target=rnfs:{book}|page=2|print=1]", first)
        self.assertNotIn("Previous", first)
        last = self.respond(GET_PAGE, target=book, print="1", page="9").decode()  # past the end: the last page
        self.assertIn("`=\nline 101\n", last)
        self.assertIn("Lines 101-120 of 120", last)
        self.assertIn("|page=2|print=1]", last)
        view = self.page(GET_PAGE, target=book, action="view", page="2", o="b.txt")
        self.assertIn("Showing b.txt", view)
        self.assertIn("\nline 51\n", view)
        self.assertNotIn("\nline 50\n", view)
        self.assertIn(f"`[Next →`:/page/get.mu`target=rnfs:{book}|page=3|action=view|o=b.txt]", view)
        short = self.hosted("short.txt", b"one\ntwo")
        self.assertEqual(self.respond(GET_PAGE, target=short, print="1"), b"`=\none\ntwo\n`=\n")  # one page
        site = self.hosted("site.mu", "\n".join(f"line {i}" for i in range(1, 121)).encode())
        page = self.respond(GET_PAGE, target=site, print="1").decode()  # a page: shown whole
        self.assertIn("line 120", page)
        self.assertNotIn("Next", page)
        self.assertIn(b"line 120", self.respond(GET_HTML, target=book, print="1"))  # HTML: whole

    def test_print_doesnt_fetch_what_it_cant_show(self):
        self.make(view_max_size=4, base64_max_size=4, allowed_identities=[ALICE.hex()])
        big = self.hosted("big.txt", b"12345")  # over view_max_size
        sealed = self.hosted("sealed.txt", b"123")
        self.api.info_table[sealed]["sealed"] = True
        for target, flags in ((big, {"print": "1"}), (big, {"action": "view"}), (big, {"base64": "1"}),
                              (sealed, {"print": "1"})):
            with self.subTest(target=target, **flags):
                self.respond(GET_PAGE, target=target, **flags)
                self.idle()
                self.assertIn(b"`[Fetch`", self.respond(GET_PAGE, target=target, **flags))
        self.assertNotIn("get", [c[0] for c in self.api.calls])

    def test_base64(self):
        self.make(auto_pin_max_size=1000, page_wait=5, base64_max_size=5, allowed_identities=[ALICE.hex()])
        photo = self.hosted("photo.jpg", b"\xff\xd8\x00")
        self.assertEqual(self.respond(GET_PAGE, target=photo, base64="1"), b"/9gA")
        self.assertEqual(self.respond(INDEX_PAGE, cid=photo, base64="yes"), b"/9gA")
        big = self.hosted("big.bin", b"123456")
        self.assertIn(b"Too big for base64", self.respond(GET_PAGE, target=big, base64="1"))
        self.assertIn(b"Saves as", self.respond(GET_PAGE, target=photo, base64="0"))  # 0: off

    # Directories

    def test_directory_listing(self):
        page = self.settle(target=ROOT)
        self.idle()
        page = self.page(GET_PAGE, target=ROOT)
        self.assertIn("2 items", page)
        self.assertIn(f"`[docs/`:/page/get.mu`target=rnfs:{ROOT}/docs]", page)
        # A name with Micron's syntax characters: harmless label, percent-encoded target
        self.assertIn(f"`[a'b)c`:/page/get.mu`target=rnfs:{ROOT}/a%60b%5Dc]", page)
        self.assertIn("program", page)

    def test_directory_pages(self):
        self.make(listing_page_size=1, allowed_identities=[ALICE.hex()])
        self.settle(target=ROOT)
        self.idle()
        page = self.page(GET_PAGE, target=ROOT, page="2")
        self.assertIn("page 2 of 2", page)
        self.assertIn("← Previous", page)
        self.assertNotIn("docs/", page)
        page = self.page(GET_PAGE, target=ROOT, page="junk")
        self.assertIn("page 1 of 2", page)

    def test_listings_kept_within_budget(self):
        self.api.listings[SUB] = [{"name": "x", "kind": "file", "size": 1, "cid": REPORT}]
        self.api.info_table[SUB] = {"cid": SUB, "type": "directory", "size": 5, "executable": False}
        with mock.patch("rnfs_nomadnet_gateway.gateway.MAX_LISTED_ENTRIES", 2):
            self.settle(target=ROOT)  # 2 entries
            self.idle()
            self.settle(target=SUB)  # 1 more: ROOT's listing is dropped
            self.idle()
        self.assertIsNone(self.gateway.entries[ROOT].listing)
        self.assertIsNotNone(self.gateway.entries[SUB].listing)
        self.assertIn("2 items", self.settle(target=ROOT))  # listed again when viewed

    def test_waiting_requests_bounded(self):
        self.make(page_wait=5, allowed_identities=[ALICE.hex()])
        self.gateway.waiting = threading.BoundedSemaphore(1)
        self.gateway.waiting.acquire()  # every waiting place taken
        self.api.gate.clear()
        start = time.monotonic()
        self.assertIn("Looking up", self.page(GET_PAGE, target=REPORT))  # answers at once
        self.assertLess(time.monotonic() - start, 2)
        self.api.gate.set()

    # Fetches

    def fetch(self, target, identity=ALICE) -> str:
        self.page(GET_PAGE, identity, target=target)
        self.idle()
        page = self.page(GET_PAGE, identity, target=target, action="fetch")
        self.idle()
        return page

    def test_fetch_and_download(self):
        target = f"{ROOT}/docs/report.pdf"
        self.api.gate.clear()
        self.page(GET_PAGE, target=target)
        self.api.gate.set()
        self.idle()
        self.api.gate.clear()
        page = self.page(GET_PAGE, target=target, action="fetch")
        self.assertIn("Fetching", page)
        self.api.gate.set()
        self.idle()
        call = self.api.calls[-1]
        self.assertEqual(call[:2], ("get", target))
        self.assertEqual(call[2], os.path.join(self.spool.directory, REPORT, "report.pdf"))
        page = self.page(GET_PAGE, target=target)
        self.assertIn(f"`[Download report.pdf`:/file/{REPORT}/report.pdf]", page)
        response = self.gateway.respond(FILE_PATH, {"var_target": f"rnfs:{ROOT}/docs/report.pdf"}, ALICE, "link")
        self.assertIs(type(response[0]), io.BufferedReader)  # RNS sends exactly this as a file
        self.assertEqual(response[1], {"name": b"report.pdf"})
        self.assertEqual(response[0].read(), b"xxxxx")
        response[0].close()
        self.assertIn("report.pdf", self.page(INDEX_PAGE))  # in recent fetches

    def test_download_paths(self):
        registered = []
        self.gateway.register_path = registered.append
        self.fetch(f"{ROOT}/docs/report.pdf")
        self.page(GET_PAGE, target=f"{ROOT}/docs/report.pdf")
        path = f"/file/{REPORT}/report.pdf"
        self.assertEqual(registered, [path])
        response = self.gateway.download(path, ALICE, "link")
        self.assertIs(type(response[0]), io.BufferedReader)
        self.assertEqual(response[1], {"name": b"report.pdf"})
        response[0].close()
        self.assertIn(b"Access needed", self.gateway.download(path, None, "link"))  # access still applies
        self.assertIn(b"Not found", self.gateway.download(f"/file/{REPORT}/other", ALICE, "link"))

    def test_download_path_for_awkward_name(self):
        # Names a client could read as URL syntax get the CID as their path's last part, not their name
        self.page(INDEX_PAGE, cid=REPORT, o="a b?c.pdf")
        self.idle()
        self.gateway.respond(GET_PAGE, {"var_target": REPORT, "var_action": "fetch", "var_o": "a b?c.pdf"}, ALICE)
        self.idle()
        page = self.page(GET_PAGE, target=REPORT, o="a b?c.pdf")
        self.assertIn(f"`[Download a b?c.pdf`:/file/{REPORT}/{REPORT}]", page)
        response = self.gateway.download(f"/file/{REPORT}/{REPORT}", ALICE, "link")
        self.assertEqual(response[1], {"name": b"a b?c.pdf"})
        response[0].close()

    def test_spooled_paths_after_restart(self):
        self.fetch(f"{ROOT}/docs/report.pdf")
        self.make(allowed_identities=[ALICE.hex()])  # a new gateway, same spool
        registered = []
        self.gateway.register_path = registered.append
        self.gateway.register_spooled()
        self.assertEqual(registered, [f"/file/{REPORT}/report.pdf"])
        response = self.gateway.download(registered[0], ALICE, "link")
        self.assertEqual(response[1], {"name": b"report.pdf"})
        response[0].close()

    def test_unsafe_name_downloads_as_cid(self):
        self.fetch(f"{ROOT}/.hidden")
        response = self.gateway.respond(FILE_PATH, {"var_target": f"{ROOT}/.hidden"}, ALICE, "link")
        self.assertEqual(response[1], {"name": REPORT.encode()})
        response[0].close()

    def test_same_cid_other_name(self):
        self.fetch(f"{ROOT}/docs/report.pdf")
        page = self.page(GET_PAGE, target=REPORT)
        self.idle()
        page = self.page(GET_PAGE, target=REPORT)
        self.assertIn("Download", page)  # already in the spool
        response = self.gateway.respond(FILE_PATH, {"var_target": REPORT}, ALICE, "link")
        self.assertEqual(response[1], {"name": REPORT.encode()})
        response[0].close()

    def test_download_not_ready(self):
        response = self.gateway.respond(FILE_PATH, {"var_target": REPORT}, ALICE, "link")
        self.assertIn(b"Not ready", response)
        response = self.gateway.respond(FILE_PATH, {"var_target": f"{ROOT}/docs/report.pdf"}, ALICE, "link")
        self.assertIn(b"Not ready", response)

    def test_bare_cid_download_after_restart(self):
        self.fetch(REPORT)
        self.make(allowed_identities=[ALICE.hex()])  # a new gateway, same spool
        response = self.gateway.respond(FILE_PATH, {"var_target": REPORT}, ALICE, "link")
        self.assertEqual(response[1], {"name": REPORT.encode()})
        response[0].close()

    def test_too_large(self):
        page = self.fetch(BIG)
        self.assertIn("Too big", page)
        self.assertNotIn("`[Fetch`", page)
        self.assertNotIn("get", [c[0] for c in self.api.calls])

    def test_rnfs_commands(self):
        self.make(auto_pin_max_size=10, allowed_identities=[ALICE.hex()])
        page = self.settle(target=f"{ROOT}/docs/report.pdf")
        self.assertIn(">>Get it with rnfs", page)
        self.assertIn("Save it as report.pdf:", page)
        self.assertIn(f"\n  rnfs get {ROOT}/docs/report.pdf\n", page)  # <cid>/<path>: rnfs get takes no rnfs: link
        self.idle()
        page = self.page(GET_PAGE, target=f"{ROOT}/docs/report.pdf")  # fetched: known to be text
        self.assertIn(f"\n  rnfs cat {ROOT}/docs/report.pdf\n", page)
        self.assertNotIn("--raw", page)
        page = self.settle(target=REPORT, o="my report.pdf")  # quoted for a shell
        self.assertIn(f"\n  rnfs get {REPORT} -o 'my report.pdf'\n", page)
        self.assertIn("--raw", self.settle(target=SEALED))
        awkward = self.hosted("it's (1).txt", b"x")
        self.assertIn(f"\n  rnfs get '{ROOT}/it'\"'\"'s (1).txt'\n", self.settle(target=awkward))

    def test_folder_files_pinned_on_their_own(self):
        target = f"{ROOT}/docs/report.pdf"  # 5 bytes, over auto_pin_max_size (0 here)
        page = self.settle(target=target)
        self.assertNotIn("own CID", page)  # looked up only: nothing fetched, nothing pinned
        self.fetch(target)
        self.assertIn(("pin", target), self.api.calls)  # the file itself (rnfs pins a path's file), not ROOT
        self.assertEqual(self.api.sources[REPORT], {"user"})
        page = self.page(GET_PAGE, target=target)
        self.assertIn("Or by the file's own CID, pinned here:", page)
        self.assertIn(f"\n  rnfs get {REPORT} -o report.pdf\n", page)
        other = cid("other")
        self.api.info_table[other] = {"cid": other, "type": "file", "size": 5, "executable": False}
        self.fetch(other)  # a bare CID is its own CID already: fetched, not pinned
        self.assertNotIn(("pin", other), self.api.calls)
        self.assertNotIn("own CID", self.page(GET_PAGE, target=other))

    def test_folder_pins_within_quota(self):
        self.make(auto_pin_quota=0, allowed_identities=[ALICE.hex()])  # 0: the gateway pins nothing
        target = f"{ROOT}/docs/report.pdf"
        self.fetch(target)
        self.assertNotIn(("pin", target), self.api.calls)
        page = self.page(GET_PAGE, target=target)
        self.assertIn("Download", page)
        self.assertNotIn("own CID", page)

    def test_sealed_uses_stored_size_and_hides_name(self):
        self.make(max_file_size=100, allowed_identities=[ALICE.hex()])
        page = self.fetch(SEALED)
        self.assertIn("Encrypted", page)
        self.assertIn("Too big", page)  # 200 stored bytes, though its plaintext is 3
        self.assertNotIn("secret.txt", page)

    def test_fetch_failure(self):
        self.api.fail_get = ApiError("no providers found", 404)
        page = self.fetch(REPORT)
        page = self.page(GET_PAGE, target=REPORT)
        self.assertIn("Fetch failed", page)
        self.assertEqual(os.listdir(self.spool.directory), [])  # no empty directory left
        gets = len([c for c in self.api.calls if c[0] == "get"])
        self.page(GET_PAGE, target=REPORT)
        self.api.fail_get = None
        self.page(GET_PAGE, target=REPORT, action="fetch")  # Retry
        self.idle()
        self.assertEqual(len([c for c in self.api.calls if c[0] == "get"]), gets + 1)
        self.assertIn("Download", self.page(GET_PAGE, target=REPORT))

    def test_hourly_limit(self):
        self.make(fetches_per_hour=1, allowed_identities=[ALICE.hex(), BOB.hex()])
        other = cid("other")
        self.api.info_table[other] = {"cid": other, "type": "file", "size": 1, "executable": False}
        self.fetch(REPORT)
        page = self.fetch(other)
        self.assertIn("No fetches left this hour", page)
        self.assertIn("Fetching", self.fetch(other, identity=BOB))  # limits are per visitor
        self.assertIn("Download", self.page(GET_PAGE, BOB, target=other))
        third = cid("third")
        self.api.info_table[third] = {"cid": third, "type": "file", "size": 1, "executable": False}
        self.assertIn("No fetches left", self.fetch(third))
        self.clock.now += 3601
        self.assertIn("Fetching", self.fetch(third))

    def test_running_limits(self):
        self.make(max_fetches=1, allowed_identities=[ALICE.hex(), BOB.hex()])
        other = cid("other")
        self.api.info_table[other] = {"cid": other, "type": "file", "size": 1, "executable": False}
        self.settle(target=REPORT)
        self.settle(target=other, identity=BOB)
        self.api.gate.clear()
        self.assertIn("Fetching", self.page(GET_PAGE, target=REPORT, action="fetch"))
        page = self.page(GET_PAGE, ALICE, target=other, action="fetch")
        self.assertIn("Wait for your other fetch", page)
        page = self.page(GET_PAGE, BOB, target=other, action="fetch")
        self.assertIn("The gateway is full", page)
        # Another visitor asking for the same file joins the running fetch
        self.assertIn("Fetching", self.page(GET_PAGE, BOB, target=REPORT, action="fetch"))
        self.api.gate.set()
        self.idle()
        self.assertEqual(len([c for c in self.api.calls if c[0] == "get"]), 1)

    def test_spool_full(self):
        self.make(spool_max=6, allowed_identities=[ALICE.hex(), BOB.hex()])
        other = cid("other")
        self.api.info_table[other] = {"cid": other, "type": "file", "size": 5, "executable": False}
        self.fetch(REPORT)
        sent = self.gateway.respond(FILE_PATH, {"var_target": REPORT}, ALICE, "link")  # being sent
        page = self.fetch(other, identity=BOB)
        self.assertIn("No room", page)
        self.spool.release("link")  # the link closed
        self.assertTrue(sent[0].closed)
        page = self.fetch(other, identity=BOB)
        self.assertIn("Fetching", page)
        self.idle()
        self.assertIsNone(self.spool.find(REPORT))  # the least recently used was removed


if __name__ == "__main__":
    unittest.main()
