# rnfs NomadNet gateway

Lets [NomadNet](https://github.com/markqvist/NomadNet) users get files from [rnfs](https://github.com/afit21/rnfs)
without running rnfs themselves.

A visitor opens the gateway in NomadNet (or MeshChat) and gives it a CID, a `<cid>/<path>`, or an `rnfs:` link. The
gateway looks the content up through the rnfs daemon on its machine, lists folders, fetches files, and hands
them to the visitor as a normal NomadNet download. Text files can also be read right in the page.

The gateway is its own NomadNet node, with its own Reticulum identity. It runs next to the rnfs daemon, on the same
machine and as the same user, and talks to it through rnfs's client library. It changes nothing in the rnfs
protocol. The full behaviour is in [specification.md](specification.md).

## Install

You need, on the machine that will run the gateway:

- rnfs 0.1.5 or newer, with its daemon running as your user
  ([rnfs's installer](https://github.com/afit21/rnfs#install-script) with `--service` sets that up)
- Linux, Python 3.11 or newer with venv (`sudo apt install python3 python3-venv` on Debian and Ubuntu)
- internet access to GitHub and PyPI while installing

Then run the installer as the user that runs the rnfs daemon (not root). Choose who may use the gateway:

```sh
# Only people you allow, by their Reticulum identity hash:
curl -fsSL https://raw.githubusercontent.com/afit21/rnfs-nomadnet-gateway/main/install.sh | sh -s -- \
    --name "My rnfs gateway" --access whitelist --allow <identity hash>

# Anyone (with per-visitor limits):
curl -fsSL https://raw.githubusercontent.com/afit21/rnfs-nomadnet-gateway/main/install.sh | sh -s -- \
    --name "My rnfs gateway" --access open
```

`--name` is the name shown in NomadNet's node list. With `--access whitelist`, give at least one `--allow`, or
`--allow-later` to allow nobody yet: a visitor who isn't allowed is shown their identity hash, which they can send
you.

The installer:

- checks that rnfs is installed and that its daemon runs as you, and finds the daemon's directory, even one
  started with `--dir`
- installs the gateway's main branch in its own venv (`~/.local/share/rnfs-nomadnet-gateway`), with rnfs's client
  library (rnfs 0.1.5's release wheel, its SHA-256 checked) and Reticulum, and links the command
  `~/.local/bin/rnfs-nomadnet-gateway`
- writes the settings to `~/.rnfs-nomadnet-gateway/config.toml` and checks the gateway can reach the rnfs daemon
- installs and starts a systemd user service, started after `rnfs.service` and `rnsd.service`
- prints the gateway's address

Your rnfs data and Reticulum config are never changed.

### Managing it

Run the installer again with what you want to change. Below, `install.sh` stands for
`curl -fsSL https://raw.githubusercontent.com/afit21/rnfs-nomadnet-gateway/main/install.sh | sh -s --`:

| Command | Does |
|---|---|
| `install.sh --allow <hash>` | Allow someone more (adds to those already allowed) |
| `install.sh --name "New name"` | Change any setting given at install, then restart |
| `install.sh --update` | Install the newest main branch, keep the settings, restart |
| `install.sh --uninstall` | Remove the gateway and its service (keeps `~/.rnfs-nomadnet-gateway`) |

Other options: `--rnfs-dir DIR` (the daemon's directory, if it isn't running while you install and doesn't use
`~/.rnfs`), `--no-service` (no systemd service: run `rnfs-nomadnet-gateway` yourself), `--python P`, and `--force`
(replace a command or service the installer didn't make). The full list is at the top of
[install.sh](install.sh).

- Logs: `journalctl --user -u rnfs-nomadnet-gateway -f`
- Keep it running while you're logged out: `loginctl enable-linger $USER`
- Check the settings and the connection to rnfs, without starting: `rnfs-nomadnet-gateway --check`

From a clone of this repository, `sh install.sh ...` takes the same options and installs that checkout instead
of the main branch.

### By hand

Install rnfs's client library (rnfs isn't on PyPI) and the gateway into a venv, from a clone of this repository:

```sh
python3 -m venv ~/gateway-venv
~/gateway-venv/bin/pip install https://github.com/afit21/rnfs/releases/download/v0.1.5/rnfs-0.1.5-py3-none-any.whl .
~/gateway-venv/bin/rnfs-nomadnet-gateway
```

Set `rnfs_dir` in the settings if your rnfs daemon doesn't use `~/.rnfs`. At its first start the gateway creates
`~/.rnfs-nomadnet-gateway/` with:

- `identity`: the gateway's Reticulum identity (not the rnfs node's)
- `config.toml`: every setting, commented out with its default
- `spool/`: fetched files waiting to be downloaded

It prints its address and announces itself with `node_name`. Options: `--dir` (another gateway directory),
`--rnsconfig` (a Reticulum config directory), `-v` (more logging), `--check`, and `--print-address` (print the
address without starting). A systemd user unit is in
[contrib/rnfs-nomadnet-gateway.service](contrib/rnfs-nomadnet-gateway.service).

Like other Reticulum programs, the gateway attaches to a shared Reticulum instance if one is running (`rnsd`, or
the rnfs daemon), and otherwise becomes one itself, so start it after them. The shared instance only answers
programs that hold its RPC key. If it was started with another config directory than the gateway's, the gateway
looks for the key itself: in the command line of the process running the instance (e.g.
`rnfs daemon --rnsconfig <dir>`), in rnfs's `reticulum_dir`, and in Reticulum's default directories. Set
`reticulum_dir` only if that fails; the log says so.

## Access

With `access = "whitelist"` (the default), only visitors whose identity hash is in `allowed_identities` can use the
gateway:

```toml
allowed_identities = ["1f2e3d4c5b6a79881f2e3d4c5b6a7988"]
```

Visitors have to turn on *Identify When Connecting* for the gateway in NomadNet (Network → Known Nodes → the
gateway → Node Info). One who isn't allowed is shown their identity hash, to send you.

With `access = "open"`, anyone may use it. Each visitor has their own limits; visitors who don't identify share
one set.

## Using it

**Opening content.** Open `<gateway address>:/page/index.mu`, or put a CID at the end:
`` <gateway address>:/page/index.mu`cid=<CID> ``. `cid=` also takes `<cid>/<path>` or an `rnfs:` link. The start
page shows these with the gateway's own address, the visitor's fetches left this hour, and their recent fetches.

**Folders** are listed 50 entries per page (`listing_page_size`).

**Files** show their name, size and CID, and a **Download** link once the gateway has the file:

- Files up to 1 MiB (`auto_pin_max_size`) are fetched straight away; a page waits up to 2 seconds (`page_wait`) so
  that a file rnfs already has opens ready to download.
- Larger files get a **Fetch** link. Big files can take a while; the page has a Refresh link.
- **Download** saves the file in the visitor's downloads. Each ready file also has its own path,
  `/file/<cid>/<name>`, which every client can follow.

**Reading text.** Text files up to 5 MiB (`view_max_size`) can be read in the page. Any valid UTF-8 counts, such as
`.txt`, `.md`, `.csv` or `.mu`:

- **View** shows the file as a page: `.mu` files as Micron, other text as written. Add `|action=view` to the
  address to open one directly.
- Add `|print=1` to get the file itself as the page, with nothing of the gateway's around it, so a site hosted in
  rnfs can be browsed through the gateway.
- Plain text over 50 lines (`view_page_lines`) is shown 50 lines at a time, with Previous and Next links;
  `|page=<n>` opens a page. `.mu` and `.html` files are shown whole.
- Asking to view or print a file fetches it first, if needed.

**More options**, added to the address the same way (as in `` <gateway address>:/page/index.mu`cid=<CID>|o=notes.txt ``):

| Option | Does |
|---|---|
| `o=<file name>` | Save the download under this name |
| `base64=1` | Get the file as base64 text alone (up to 1 MiB, `base64_max_size`), e.g. for a `data:` image |
| `html=1` | Get the page as HTML |

Every page also comes as HTML, at `/page/index.html` and `/page/get.html`; MeshChatX shows a page whose path ends
in `.html` as a web page. Turn this off with `html_pages = false`.

**Getting it with rnfs instead.** A file's page shows the `rnfs get` command (and, for text, `rnfs cat`) that gets
it on a computer running rnfs.

**Pins.** The gateway pins files on your rnfs node so they stay available:

- files up to 1 MiB, as they're fetched
- any file a visitor fetches from inside a folder, whatever its size. rnfs pins the file on its own, under its own
  CID, and provides it, so that CID alone gets the file: its page then shows `rnfs get <file CID> -o <name>`.

The gateway's pins are kept within 4 GiB (`auto_pin_quota`), releasing the least recently used first; 0 turns
pinning off. Your own pins are never released.

**Linking from other pages.** Other NomadNet pages can link to files through the gateway:

```
`[Get this file`<gateway address>:/page/get.mu`target=<cid>]
`[Get this file`<gateway address>:/page/index.mu`cid=<cid>|o=<file name>]
```

A download name that rnfs wouldn't use by default (hidden, starting with `-`, or a path) is ignored.

Sealed (encrypted) files are always fetched as stored: the gateway never decrypts them, even ones sealed for its
own rnfs node. Name links (`rnfs:name/…`) aren't supported yet.

## Settings

In `~/.rnfs-nomadnet-gateway/config.toml`. Times are in seconds and sizes in bytes, as in `rnfs.toml`. Restart the
gateway after editing it (`systemctl --user restart rnfs-nomadnet-gateway`).

| Setting | Default | |
|---|---|---|
| `node_name` | `"rnfs gateway"` | The name it announces |
| `announce_interval` | 21600 (6 h) | |
| `rnfs_dir` | `"~/.rnfs"` | The rnfs daemon's `--dir`, where its API token and `rnfs.toml` are. The installer sets it to your running daemon's |
| `api_port` | from `rnfs.toml`, else 8788 | |
| `reticulum_dir` | Reticulum's own | |
| `access` | `"whitelist"` | `"whitelist"` or `"open"` |
| `allowed_identities` | `[]` | Identity hashes, 32 hex characters |
| `spool_max` | 1073741824 (1 GiB) | Fetched files kept for download; least recently used removed first |
| `spool_ttl` | 604800 (7 days) | Since a file was last fetched or downloaded |
| `max_file_size` | 104857600 (100 MiB) | Largest file a visitor may have fetched (rnfs's `max_download_size` applies too) |
| `max_fetches` | 2 | Fetches running at once |
| `max_fetches_per_visitor` | 1 | |
| `fetches_per_hour` | 10 | Per visitor |
| `max_workers` | 2 | Lookups, listings and fetches run against rnfs at once |
| `listing_page_size` | 50 | Folder entries per page |
| `failure_ttl` | 300 (5 min) | How long a failed lookup or fetch is remembered |
| `page_wait` | 2 | Seconds a page waits for a lookup or fetch before showing Refresh (0-10) |
| `auto_pin_max_size` | 1048576 (1 MiB) | Files up to this are fetched and pinned without being asked; 0: never |
| `auto_pin_quota` | 4294967296 (4 GiB) | Bytes the gateway's own pins may hold; least recently used released first; 0: none |
| `view_max_size` | 5242880 (5 MiB) | Text files up to this can be viewed or printed; 0: never |
| `view_page_lines` | 50 | Plain text is shown this many lines at a time (1-1000) |
| `base64_max_size` | 1048576 (1 MiB) | Files up to this can be had as base64 text; 0: never |
| `html_pages` | `true` | Also serve every page as HTML |

## Development

The tests need rnfs's client library, which isn't on PyPI:

```sh
python3 -m venv .venv
.venv/bin/pip install https://github.com/afit21/rnfs/releases/download/v0.1.5/rnfs-0.1.5-py3-none-any.whl -e .
.venv/bin/python -m unittest
```

## Known limitations

- Fetches show no progress, and a restarted gateway starts its fetches again: rnfs's background jobs can't yet
  fetch sealed files as stored, which the gateway needs (specification section 9).
- Tested with NomadNet 1.4.4. Other clients, such as MeshChat, are less tested.
- Name links (`rnfs:name/…`) aren't supported yet.
