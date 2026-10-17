# rnfs NomadNet gateway

A companion application for [rnfs](https://github.com/afit21/rnfs) that lets NomadNet users get rnfs content
without running rnfs. A visitor gives the gateway a CID (or `<cid>/<path>`, or an `rnfs:` link); the gateway
fetches it through the local rnfs daemon, and the visitor downloads the file with their NomadNet client.

It runs as its own NomadNet node (`nomadnetwork.node`, with its own RNS identity), next to the rnfs daemon on the
same machine and as the same user. It changes nothing in the rnfs protocol. See [specification.md](specification.md).

## Install and run

On a machine where rnfs is installed and its daemon runs, run the installer from this directory, as the user that
runs the rnfs daemon (not root):

```sh
sh install.sh --name "My rnfs gateway" --access whitelist --allow <your identity hash>
sh install.sh --name "My rnfs gateway" --access open          # or: anyone may use it
```

`--name` and `--access` are required the first time; with `--access whitelist`, so is at least one `--allow`
(or `--allow-later`: a visitor who isn't allowed is shown their identity hash, which they send you). The installer:

- checks that rnfs is installed and that its daemon runs as you (it stops if the daemon runs as another user),
  and finds the daemon's directory, even one started with `--dir` (saved as `rnfs_dir`; `--update` saves it again)
- installs the gateway in its own venv (`~/.local/share/rnfs-nomadnet-gateway`), with the command
  `~/.local/bin/rnfs-nomadnet-gateway`, with rnfs's client library (rnfs 0.1.5's release wheel, its hash
  checked) and Reticulum (needs Python 3.11+ with venv, and internet access to GitHub and PyPI)
- writes `~/.rnfs-nomadnet-gateway/config.toml` and checks the gateway can use the rnfs daemon's API
- installs and starts a systemd user service, ordered after `rnfs.service` and `rnsd.service`, and prints the
  gateway's address

Later: `sh install.sh --allow <hash>` allows someone more (any setting can be changed so), `sh install.sh --update`
installs a new version and restarts it, and `sh install.sh --uninstall` removes it (keeping
`~/.rnfs-nomadnet-gateway`). `sh install.sh --help` lists every option. To keep the gateway running while you're
logged out: `loginctl enable-linger $USER`. Logs: `journalctl --user -u rnfs-nomadnet-gateway -f`.

`rnfs-nomadnet-gateway --check` checks the settings and that the rnfs daemon answers, without starting.

### By hand

Needs Python 3.11+, a running `rnfs daemon` (0.1.5 or newer) for the same user, rnfs's client library (`pip install
https://github.com/afit21/rnfs/releases/download/v0.1.5/rnfs-0.1.5-py3-none-any.whl`: rnfs isn't on PyPI), and
Reticulum (it attaches to `rnsd` if one runs). Set `rnfs_dir` if the daemon doesn't use `~/.rnfs`.

Like other RNS programs, it attaches to a shared Reticulum instance if one is running (`rnsd`, or the rnfs daemon),
and otherwise becomes one itself. The shared instance only answers programs that hold its RPC key, which comes from
the config directory it was started with; if that isn't the gateway's, the gateway finds it: in the command line of
the process running the instance (e.g. `rnfs daemon --rnsconfig <dir>`), rnfs's `reticulum_dir`, and RNS's default
directories. Setting `reticulum_dir` is only needed if that fails (the log says so). Start the gateway after the
program that runs the shared instance, or it becomes the shared instance itself, with its own config.

```sh
pipx install .
rnfs-nomadnet-gateway            # or: python3 -m rnfs_nomadnet_gateway
```

At its first start it creates `~/.rnfs-nomadnet-gateway/` with:

- `identity`: the gateway's RNS identity (not the rnfs node's)
- `config.toml`: every setting, commented out with its default
- `spool/`: fetched files waiting to be downloaded

It prints its node address and announces itself with `node_name`, so it shows up in NomadNet's node list.
`rnfs-nomadnet-gateway --print-address` prints the address without starting.

Options: `--dir` (another gateway dir), `--rnsconfig` (Reticulum config dir), `-v` (more logging), `--check`.
A systemd user unit is in [contrib/rnfs-nomadnet-gateway.service](contrib/rnfs-nomadnet-gateway.service).

## Access

By default (`access = "whitelist"`) only visitors whose RNS identity hash is in `allowed_identities` can use the
gateway. Visitors must turn on *Identify When Connecting* for the gateway in NomadNet (Network → Known Nodes →
Node Info). A visitor who isn't allowed gets a page that shows their identity hash, which they can send to you:

```toml
allowed_identities = ["1f2e3d4c5b6a79881f2e3d4c5b6a7988"]
```

With `access = "open"` anyone may use it; unidentified visitors share one set of limits.

## Using it

- Open `<gateway address>:/page/index.mu`cid=<CID>` (in MeshChat, in its address bar). `cid=` also takes
  `<cid>/<path>` or an `rnfs:` link; add `|o=<file name>` to name the download. The start page shows these with
  the gateway's own address, plus the visitor's remaining fetches and recent fetches.
- A folder is listed (`listing_page_size` entries per page). A file shows its name, size and CID, and the
  `rnfs get` (and, for text, `rnfs cat`) command that gets it with rnfs itself.
- Files up to `auto_pin_max_size` (1 MiB) are fetched and pinned on the rnfs node straight away: a page waits up
  to `page_wait` (2 s) for that, so a file the node has opens with its **Download** link, no Refresh needed.
  Larger files get a **Fetch** link and are only fetched when asked: by the Fetch link, or by asking to see
  the file (View, `print=1`, `base64=1`), which fetches it if it is small enough to be shown so.
- A file fetched from inside a folder is pinned on its own, whatever its size: rnfs pins the file itself under
  its own CID (not the folder), and provides it, so that CID alone gets the file. Its page then shows
  `rnfs get <its CID> -o <name>`. The gateway's pins are kept within `auto_pin_quota` (4 GiB), releasing the
  least recently used first (0: the gateway pins nothing); your own pins are never touched.
- Text files (`.txt`, `.md`, `.mu`, any valid UTF-8) up to `view_max_size` (5 MiB) get a **View** link that
  shows them as a page (Micron for `.mu`, plain text otherwise). Add `|action=view` to the address to open one
  directly. Plain text over `view_page_lines` (50) lines is shown that many lines at a time, with Previous and
  Next links (`|page=<n>` opens a page).
- Add `|print=1` to show a file of up to `view_max_size` as the page itself, with nothing of the gateway's
  around it but, for plain text over `view_page_lines` lines, the line count and Previous and Next links: a
  hosted `.mu` page opens as a page, whole (on `/page/get.html`, an `.html` file opens as HTML, and text whole). Add
  `|base64=1` to get a file of up to `base64_max_size` (1 MiB) as base64 text alone, e.g. for a `data:` image.
- Every page also comes as HTML, at `/page/index.html` and `/page/get.html` (or add `|html=1`): MeshChatX shows
  a page whose path ends in `.html` as a web page. The start page links to it. Turn off with `html_pages`.
- **Download** saves the file in the visitor's downloads. Each ready file has its own path,
  `/file/<cid>/<name>`, which every client can follow.
- Other pages can link to the gateway: `` `[Get this file`<gateway address>:/page/get.mu`target=<cid>] ``, or
  `` `[Get this file`<gateway address>:/page/index.mu`cid=<cid>|o=<file name>] ``. `o` is optional and names the
  download; a name rnfs wouldn't use as a default output name (hidden, starting with `-`, a path) is ignored.

Sealed files are always fetched raw: the gateway never decrypts them, even ones addressed to its rnfs node.
Name targets (`name/…`) are not supported yet.

## Settings

In `<gateway dir>/config.toml`. Times are in seconds and sizes in bytes, as in `rnfs.toml`.

| Setting | Default | |
|---|---|---|
| `node_name` | `"rnfs gateway"` | Announced name |
| `announce_interval` | 21600 (6 h) | |
| `rnfs_dir` | `"~/.rnfs"` | The rnfs daemon's `--dir`: where its API token and `rnfs.toml` are. `install.sh` sets it to your running daemon's |
| `api_port` | from `rnfs.toml`, else 8788 | |
| `reticulum_dir` | RNS's own | |
| `access` | `"whitelist"` | `"whitelist"` or `"open"` |
| `allowed_identities` | `[]` | Identity hashes, 32 hex characters |
| `spool_max` | 1073741824 (1 GiB) | Least recently used files are removed first |
| `spool_ttl` | 604800 (7 days) | Since a file was last fetched or downloaded |
| `max_file_size` | 104857600 (100 MiB) | Largest file a visitor may have fetched (rnfs's `max_download_size` applies too) |
| `max_fetches` | 2 | Fetches running at once |
| `max_fetches_per_visitor` | 1 | |
| `fetches_per_hour` | 10 | Per visitor |
| `max_workers` | 2 | Lookups, listings and fetches run against the rnfs API at once |
| `listing_page_size` | 50 | |
| `failure_ttl` | 300 (5 min) | How long a failed lookup or fetch is remembered |
| `auto_pin_max_size` | 1048576 (1 MiB) | Files up to this are fetched and pinned on the rnfs node without being asked; 0: never |
| `page_wait` | 2 | Seconds a page waits for a lookup or fetch before showing Refresh (0-10) |
| `auto_pin_quota` | 4294967296 (4 GiB) | Bytes the gateway's own pins (small files, and files fetched from inside folders) may hold; the least recently used are released first; 0: none |
| `view_max_size` | 5242880 (5 MiB) | Text files (`.txt`, `.md`, `.mu`, …) up to this can be viewed as a page, or shown as the page itself (`print=1`); 0: never |
| `view_page_lines` | 50 | Plain text over this many lines is viewed (and printed, as Micron) a page of them at a time (1-1000) |
| `base64_max_size` | 1048576 (1 MiB) | Files up to this can be had as base64 text (`base64=1`); 0: never |
| `html_pages` | `true` | Also serve every page as HTML (`/page/index.html`, `/page/get.html`, `html=1`) |

## Tests

```sh
python3 -m unittest discover tests
```

## Known limitations

- Fetches use the blocking `POST /api/get`, so there is no progress, and a restarted gateway starts its fetches
  again. rnfs get jobs can't be used until they take `raw` (specification section 9).
- Tested against NomadNet 1.4.4's request format. Other clients, such as MeshChat, are untested.
