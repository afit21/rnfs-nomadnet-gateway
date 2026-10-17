# rnfs NomadNet gateway: specification

Status: draft. The gateway is a companion application, not part of rnfs. It changes nothing in the rnfs protocol.
Section numbers like "rnfs §11" refer to the rnfs [specification](../specification.md).

## 1. Purpose and scope

The gateway lets NomadNet users get rnfs content without running rnfs. A visitor gives the gateway a CID; the
gateway fetches it over rnfs, and the visitor downloads the file with their NomadNet client.

- **In scope:** looking up a target (`<cid>[/path]` or an `rnfs:` link), fetching it from the rnfs network,
  listing a requested directory, and downloading files.
- **Out of scope for now:** listing the node's own pins (the file-index page does that), pinning, following or
  keeping names, `name/<name>` targets, LXMF, and uploads (Micron forms can't upload files). A programmatic
  API over Reticulum is a separate rnfs feature (the RNS API on the rnfs roadmap).

## 2. Architecture

- A standalone Python program in its own process, on the same machine and OS user as the rnfs daemon.
- It talks to rnfs only through the local API (rnfs §11), with rnfs's client library: `rnfs.Client(rnfs_dir,
  port=api_port)`, which reads the port from `<rnfs dir>/rnfs.toml` and the token from `<rnfs dir>/api_token`,
  and has the daemon prove it knows the token on each request before sending it. Its errors (`rnfs.ApiError` and
  its subclasses, with the HTTP status) are what visitors are told (section 5). Targets, CIDs, entry names and
  default output names are checked by rnfs's own rules (`rnfs.features.lxmf`, `rnfs.content.dag`,
  `rnfs.fsutil`). The library's API is provisional until rnfs 0.2, so the gateway requires `rnfs>=0.1.5,<0.2`.
- It has its own RNS identity (`<gateway dir>/identity`), not the rnfs node's, and one `IN`/`SINGLE`
  destination `nomadnetwork.node`, so NomadNet clients list it as a node. It announces with `app_data` set to
  the UTF-8 node name, as NomadNet nodes do: at startup, then every `announce_interval`.
- It attaches to a shared RNS instance (`rnsd`) if there is one, like rnfs.
- **Why not page scripts on a stock NomadNet node:** NomadNet registers download paths only when it scans its
  `files` folder (at startup, or every `file_refresh_interval`, which is off by default) and never deregisters
  them, so files fetched on demand wouldn't be downloadable until the next scan.
- **Why not inside the rnfs daemon:** rnfs is the core and user-facing features are companion apps; and rnfs
  turns RNS compression off for its whole process, which would send pages uncompressed.

## 3. NomadNet compatibility

Checked against NomadNet 1.4.4. Other clients (e.g. MeshChat) are expected to follow it, but are untested.

- Requests are RNS link requests. RNS finds a handler by the hash of the request path, so every path is fixed;
  what varies travels in the request data: a dict of `var_<name>` (link variables) and `field_<name>`
  (form fields), strings. RNS reads a request whole before the handler checks access, so the destination
  refuses requests over 64 KiB (`set_max_request_size`).
- Memory and threads stay bounded whatever visitors ask for: at most 2000 targets' states, 100,000 directory
  entries across kept listings (the least recently listed are dropped and listed again when viewed), 16 requests
  waiting (`page_wait`) at once, and 10,000 pin records.
- A page response is Micron markup as bytes, which the client displays.
- The client treats a link whose path starts with `/file/` as a download, sending its link variables too. A
  response `[file, {"name": <UTF-8 bytes>}]`, where `file` is a file opened with `open(path, "rb")`
  (RNS sends a response as a file only if it is exactly an `io.BufferedReader`), is saved in the visitor's
  downloads under that name. A bytes response to a `/file/` request is displayed as a page instead, which the
  gateway uses for "not ready" and errors.
- A client identifies on connect (`Link.identify`) when identification is turned on for that node; the handler
  then gets `remote_identity`.
- Every page is built once, as Micron, and converted for the HTML version: text HTML-escaped, colours only as
  hex digits, links only to node paths (`/page/…`, `:/page/…`, `<hash>:/…`; the gateway's own pages to their
  `.html` paths). A paragraph holding a form field is left out, since an HTML page can't send one.
- Links to the gateway from other pages: `` `[Get this file`<gateway hash>:/page/get.mu`target=<cid>] ``, or
  `` `[Get this file`<gateway hash>:/page/index.mu`cid=<cid>|o=<file name>] `` (`o` optional).
- `o=<file name>` names the download. It must pass rnfs's default-name rule (section 6) and hold none of
  `` ` ``, `|`, `=` or `]`, since links carry it on; otherwise it is ignored and the page says so.

**Paths**

| Path | Request data | Response |
|---|---|---|
| `/page/index.mu` | none; or `cid`, optional `o` | Start page: how to look something up by address (`cid=`, `o=` and the flags below; there is no form, since clients send forms differently), the visitor's access (section 4), their recent fetches, and the project's GitHub. With `cid`, the same as `/page/get.mu` |
| `/page/get.mu` | `target` (var or field) or `cid`; optional `action=fetch` or `view`, `page`, `o`, `print=1`, `base64=1` | The target's state (section 5): looking up, file details with a fetch or download link, fetching, a directory listing, or an error |
| `/page/index.html`, `/page/get.html` | as their `.mu` pages | The same pages as HTML (`html_pages`), for clients that render a page by its path's ending (MeshChatX renders `.html` as sanitised HTML). `html=1` on any page also answers in HTML |
| `/file/get` | `target` or `cid` (var); optional `o` | The spooled file (section 6), or a page saying why not |
| `/file/<cid>/<name>` | | The spooled file under the download name; registered for each file as it is offered, and for the spool's files at startup. Download links use it: some clients (MeshChat 2.4.0) send a `/file/` link's variables as part of its path. `<name>` is the download name if it holds only `A-Za-z0-9._()+-`, else the CID |

## 4. Access

- `access = "whitelist"` (default): every path requires an identified visitor whose identity hash is in
  `allowed_identities`. Others get a page explaining how to turn on identification for this node in their
  client, and, if they did identify, showing their identity hash to send to the operator. An empty whitelist
  is logged as a warning at startup, since nobody can use the gateway.
- `access = "open"`: anyone may use it. Unidentified visitors share one set of limits (section 7).
- Access is checked in the gateway's handlers (registered `ALLOW_ALL`) rather than by RNS `ALLOW_LIST`, so a
  refused visitor gets an explanation instead of a request that silently fails.
- Hop count is not used: RNS doesn't record it for incoming links, the packet header field isn't authenticated,
  and through a shared instance every link arrives on the local interface.
- The gateway MUST NOT decrypt sealed files: it always fetches with `raw` (section 5), so a sealed file
  addressed to the gateway's rnfs node is downloaded encrypted.

## 5. Lookups and fetches

- **Targets.** `<cid>`, `<cid>/<path>`, or an `rnfs:` URI (rnfs §13) whose target is one of those. CIDs are
  checked against rnfs's written form (`b` + lowercase base32) before anything reaches the API; nicks, names
  and anything else are refused.
- **Pages wait on the network only briefly.** Every API call that may fetch (`/api/info`, `/api/ls`,
  `/api/pin/add`, `/api/get`) runs on a worker pool (`max_workers`), and page handlers read its results from
  memory. A page waits up to `page_wait` seconds for a lookup, listing or fetch it started (so content the rnfs
  node has is shown without a Refresh), then says it isn't ready, with a Refresh link. A handler holds up the
  RNS thread that called it while it waits, as NomadNet's page scripts do, so `page_wait` is at most 10 s.
- **States** of a target, keyed by its normalized form:
  1. *Looking up*: `GET /api/info?target=` gives its CID, type, size and execute bit.
  2. *File*: shows its name, size and CID, and how to get it with rnfs: `rnfs get <cid>/<path>` (rnfs get takes
     no rnfs: link; quoted for a POSIX shell, `-o <name>` if `o` was given), `--raw` too for a sealed file, and
     `rnfs cat` once it is known to be text. Shown on its fetching and fetch-failed pages too; left out if the
     target holds a character that can't be shown. If it is in the spool, a download link (`/file/get`); otherwise a
     Fetch link (`action=fetch`), if the size is within `max_file_size` and the visitor's limits allow it.
     A file of at most `auto_pin_max_size` is fetched without being asked (counting against the visitor's
     limits; if they don't allow it, the Fetch link is shown); larger ones only when the visitor asks, so
     looking one up never costs more than a few blocks. Asking to see the file (`action=view`, `print=1`,
     `base64=1`) asks for it to be fetched too, if it is within the limit for being shown so (and not sealed,
     but for `base64=1`); a page while it is fetching carries the request on in its Refresh and Try again
     links, so the file is shown once ready.
  3. *Fetching*: `POST /api/get {"target", "output": <spool path>, "raw": true}`. The daemon writes the output
     under a temporary name and renames it when complete, so the spool never holds a partial file.
  4. *Ready*: the download link, and for a text file of at most `view_max_size` (valid UTF-8, not sealed) a View
     link (`action=view`), which shows its content as a page: a `.mu` file (by its download or path name) as
     Micron, without page directives (`#!`), partials or images, which a client would load by itself from
     wherever they point (judged after any leading `<`, `>` or `\`, which NomadNet reads past), and with a
     literal block or table left open closed; any other as written, in a literal block, `view_page_lines` lines
     at a time (`page`, from 1; past the last, the last), then which lines they are and Previous and Next
     links carrying `action=view` and `o`, if there are more. Control characters are
     removed, and the page sets empty colour directives first, since NomadNet takes the first `#!bg=` and `#!fg=`
     anywhere in a page. An `.html` file's View link opens it with `print=1` on `/page/get.html`.
  5. *Sent as it is*, once ready, when asked for:
     - `print=1`: a text file of at most `view_max_size` is the response itself, with nothing of the gateway's
       around it, so a hosted site's page can be opened through the gateway. On a `.mu` path: a `.mu` file as
       Micron (its page directives kept; partials and images dropped, as above), whole; any other as written in
       a literal block, paged as a View is (its links carrying `print=1`). On an `.html` path: an `.html` file as HTML (lines a Micron client would read as a
       partial dropped), a `.mu` file converted to HTML, any other in a `<pre>`.
     - `base64=1`: a file of at most `base64_max_size`, as its stored bytes, base64-encoded: the response is
       that text alone, e.g. for embedding as a `data:` URI (MeshChatX's HTML keeps only those images).
     A file that can't be sent so shows its page with a note why. Flags are on unless `0`, `no`, `false`
     or `off`.
  5. *Directory*: `GET /api/ls?target=` lists its entries, `listing_page_size` per page. A subdirectory links to
     its own listing and a file to its file page, each by `<cid>/<path>`.
- **Failures** (no providers found, too large, storage full, invalid content, on the rnfs node's deny list) are shown with a Retry link and
  remembered for `failure_ttl`, so refreshing doesn't repeat them.
- **Later, once rnfs get jobs take `raw`:** fetch with `POST /api/jobs` instead, to show progress and resume
  fetches across restarts (section 9).

## 6. Spool

- Fetched files are written by the daemon to `<gateway dir>/spool/<target CID>/<name>`. The name is the target's
  last path name if it passes rnfs's default-name rule (`get` in rnfs §11), else the CID.
- `/file/get` answers `[open(path, "rb"), {"name": name}]`, where name is `o` if given and fit, else the spool
  name rule above. Names are also sanitized by the client.
- The spool is limited by `spool_max` (least recently downloaded removed first) and `spool_ttl`. A file isn't
  removed while it is being sent.
- A file of at most `auto_pin_max_size`, and any file fetched from inside a folder (a target with a path), is
  pinned on the rnfs node (`rnfs.Client.pin`, source `user`) before it is fetched into the spool, so the node
  keeps it and later lookups and fetches are local. rnfs pins a path's file as itself, under its own CID, and
  provides that CID, so the file can be had without its folder: once the pin is complete, the file's page adds
  `rnfs get <file CID> -o <name>`. Other files (bare CIDs over `auto_pin_max_size`) stay in rnfs as evictable
  cache.
- **Pin quota.** The gateway's own pins hold at most `auto_pin_quota` bytes (content sizes). It records the pins
  it adds in `<gateway dir>/pins.json`, with when each was last looked up or downloaded, and when a new pin would
  go over the quota, it releases the least recently used (`POST /api/pin/rm`, which removes only the `user`
  source). A file already pinned `user` when the gateway would pin it is the operator's: it is neither pinned
  nor recorded, so never released. A file larger than the quota isn't pinned (it is still fetched).

## 7. Limits

| Setting | Default | Meaning |
|---|---|---|
| `max_file_size` | 100 MiB | Largest file a visitor may have fetched; the daemon's `max_download_size` also applies |
| `max_fetches` | 2 | Fetches running at once |
| `max_fetches_per_visitor` | 1 | Fetches one visitor may have running |
| `fetches_per_hour` | 10 | Fetches one visitor may start per hour |
| `max_workers` | 2 | Lookups, listings and fetches the gateway runs against the API at once |
| `listing_page_size` | 50 | Directory entries per page |
| `failure_ttl` | 5 min | How long a failed lookup or fetch is remembered |
| `auto_pin_max_size` | 1 MiB | Files up to this are fetched and pinned without being asked; 0: never |
| `page_wait` | 2 s | How long a page waits for a lookup or fetch before showing Refresh (0-10 s) |
| `auto_pin_quota` | 4 GiB | Bytes the gateway's own pins (small files, and files fetched from inside folders) may hold; the least recently used are released first; 0: none |
| `view_max_size` | 5 MiB | Text files (`.txt`, `.md`, `.mu`, …) up to this can be viewed as a page (`action=view`) or sent as it is (`print=1`); 0: never (at most 16 MiB) |
| `view_page_lines` | 50 | Plain text is viewed, and printed as Micron, this many lines at a time (1-1000) |
| `base64_max_size` | 1 MiB | Files up to this can be had as base64 text (`base64=1`); 0: never |
| `html_pages` | true | Also serve every page as HTML (`/page/index.html`, `/page/get.html`, `html=1`) |

A visitor is an identity hash; in open mode every unidentified visitor counts as one.

Names and text from DAGs are untrusted: control characters are removed and Micron's formatting characters
(`` ` `` and `\`) escaped before they appear on a page, as the existing companion pages do.

## 8. Configuration

`<gateway dir>/config.toml`; the gateway dir defaults to `~/.rnfs-nomadnet-gateway`.

| Setting | Default | Meaning |
|---|---|---|
| `node_name` | `"rnfs gateway"` | Announced name |
| `announce_interval` | 6 h | |
| `rnfs_dir` | `~/.rnfs` | The rnfs daemon's `--dir`: where the API token is, and `rnfs.toml` for the API port. `install.sh` sets it to the `--dir` of this user's running `rnfs daemon` (read from its command line) |
| `api_port` | from `rnfs.toml`, else 8788 | |
| `reticulum_dir` | RNS's own | |
| `access` | `"whitelist"` | `"whitelist"` or `"open"` |
| `allowed_identities` | `[]` | Identity hashes (32 hex characters) |
| `spool_max` | 1 GiB | |
| `spool_ttl` | 7 days | |

Plus the limits in section 7.

## 9. Changes wanted in rnfs

- **`raw` for get jobs** (`POST /api/jobs`): recommended. Jobs currently decrypt sealed files the node can open,
  so the gateway can't use them. With `raw`, fetches get progress and survive restarts; without it the gateway
  uses the blocking `/api/get` and a restarted gateway starts its fetches again.

## 10. Later

- `name/<name>` targets, through `/api/name/resolve`.
- Whole directories as an rnfs archive (`export`), for visitors who run rnfs.
- Links from the file-index page to `/page/get.mu`.
- Talking to rnfs through the RNS API instead of the local API, so the gateway can run on another device.
