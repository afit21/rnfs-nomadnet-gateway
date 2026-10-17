#!/bin/sh
# Install, update or uninstall the rnfs NomadNet gateway for this user, next to an rnfs that is already
# installed. Run it from the gateway's source directory, as the user that runs the rnfs daemon (not root):
#
#   sh install.sh --name "My rnfs gateway" --access whitelist --allow <identity hash> [--allow <hash> ...]
#   sh install.sh --name "My rnfs gateway" --access whitelist --allow-later
#   sh install.sh --name "My rnfs gateway" --access open
#   sh install.sh --allow <hash>  allow someone more, once installed (any setting above can be changed so)
#   sh install.sh --update        re-install the code, keep the settings, restart the gateway
#   sh install.sh --uninstall     remove the code and the service (keeps ~/.rnfs-nomadnet-gateway)
#
# Required for the first install (later, only what you change):
#   --name NAME           the name the gateway announces, shown in NomadNet's node list
#   --access MODE         whitelist: only the identities you allow can use it
#                         open: anyone can (each limited per hour; unidentified visitors share one limit)
#   --allow HASH          (whitelist) a visitor's RNS identity hash, 32 hex characters; repeat for more. Running
#                         this again with more --allow adds them to the ones already allowed
#   --allow-later         (whitelist) allow nobody yet: a visitor who isn't allowed is shown their identity
#                         hash to send you; add it later with: sh install.sh ... --allow HASH
#
# Optional:
#   --rnfs-dir DIR        the rnfs daemon's directory (its --dir), if the daemon isn't running now and doesn't
#                         use ~/.rnfs. Otherwise it is found from the running daemon
#   --no-service          don't install or start the systemd user service (run rnfs-nomadnet-gateway yourself)
#   --python P            use this Python (3.11 or newer) for the venv
#   --force               replace an rnfs-nomadnet-gateway command or systemd unit this script didn't make
#                         (e.g. a pipx install or contrib/rnfs-nomadnet-gateway.service copied by hand)
#
# Installs:  venv ~/.local/share/rnfs-nomadnet-gateway/venv, command ~/.local/bin/rnfs-nomadnet-gateway,
#            systemd user unit ~/.config/systemd/user/rnfs-nomadnet-gateway.service (started after rnfs.service).
# Settings, identity and spool are in ~/.rnfs-nomadnet-gateway; your rnfs data and Reticulum config are never
# touched. Needs Python 3.11+ with venv and pip, and internet access to fetch rnfs's client library (the rnfs
# release's wheel, its hash checked) from GitHub and Reticulum (rns) from PyPI.
#
# Environment: GATEWAY_SOURCE  what to install instead of this script's directory: a source directory, a
#                              .zip/.tar.gz/.whl path or URL, or another pip requirement
set -eu

NAME=""
ACCESS=""
ALLOW=""  # comma-separated identity hashes
ALLOW_LATER=no
RNFS_DIR_GIVEN=""
SERVICE=yes
PYTHON=""
FORCE=no
ACTION=""  # install, update or uninstall

say() { printf '%s\n' "$*"; }
warn() { printf 'gateway installer: warning: %s\n' "$*" >&2; }
die() { printf 'gateway installer: %s\n' "$*" >&2; exit 1; }
usage() {
    if [ -f "$0" ]; then sed -n '2,/^set -eu$/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
    else say "See install.sh in the rnfs-nomadnet-gateway source"; fi
}

need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value (try --help)"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --name) need_value "$@"; NAME=$2; shift ;;
        --name=*) NAME=${1#--name=} ;;
        --access) need_value "$@"; ACCESS=$2; shift ;;
        --access=*) ACCESS=${1#--access=} ;;
        --allow) need_value "$@"; ALLOW="$ALLOW,$2"; shift ;;
        --allow=*) ALLOW="$ALLOW,${1#--allow=}" ;;
        --allow-later) ALLOW_LATER=yes ;;
        --rnfs-dir) need_value "$@"; RNFS_DIR_GIVEN=$2; shift ;;
        --rnfs-dir=*) RNFS_DIR_GIVEN=${1#--rnfs-dir=} ;;
        --no-service) SERVICE=no ;;
        --python) need_value "$@"; PYTHON=$2; shift ;;
        --python=*) PYTHON=${1#--python=} ;;
        --force) FORCE=yes ;;
        --update) ACTION=update ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (try --help)" ;;
    esac
    shift
done
ALLOW=${ALLOW#,}
[ -n "$ACTION" ] || ACTION=install

# Paths. The source is resolved before leaving the caller's directory: Python puts the current directory first
# on sys.path for -c and -m, so every Python call below runs from / and with -I
case "${HOME:-}" in /*) ;; *) die "HOME isn't set to an absolute path" ;; esac
abs_dir() { (CDPATH='' cd -P -- "$1" && pwd -P); }
SOURCE=${GATEWAY_SOURCE:-}
if [ -z "$SOURCE" ] && [ -f "$0" ]; then SOURCE=$(abs_dir "$(dirname -- "$0")"); fi
if [ -n "$SOURCE" ] && [ -d "$SOURCE" ]; then SOURCE=$(abs_dir "$SOURCE"); fi
if [ -n "$RNFS_DIR_GIVEN" ]; then
    case "$RNFS_DIR_GIVEN" in "~"|"~/"*) RNFS_DIR_GIVEN=$HOME${RNFS_DIR_GIVEN#\~} ;; esac
    [ -d "$RNFS_DIR_GIVEN" ] || die "--rnfs-dir $RNFS_DIR_GIVEN isn't a directory"
    RNFS_DIR_GIVEN=$(abs_dir "$RNFS_DIR_GIVEN")
fi
case "$PYTHON" in */*) [ -e "$PYTHON" ] && PYTHON=$(abs_dir "$(dirname -- "$PYTHON")")/$(basename -- "$PYTHON") ;; esac
case "${XDG_DATA_HOME:-/}" in /*) ;; *) unset XDG_DATA_HOME ;; esac
case "${XDG_CONFIG_HOME:-/}" in /*) ;; *) unset XDG_CONFIG_HOME ;; esac
cd /

PREFIX=${XDG_DATA_HOME:-$HOME/.local/share}/rnfs-nomadnet-gateway
VENV=$PREFIX/venv
BIN_DIR=$HOME/.local/bin
LAUNCHER=$BIN_DIR/rnfs-nomadnet-gateway
UNIT_NAME=rnfs-nomadnet-gateway.service
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT=$UNIT_DIR/$UNIT_NAME
UNIT_MARK="installed by rnfs-nomadnet-gateway's install.sh"
GATEWAY_DIR=$HOME/.rnfs-nomadnet-gateway
PIP_OPTS="--disable-pip-version-check"
# The rnfs release whose client library (rnfs.Client) the gateway uses, and its wheel's SHA-256 (SHA256SUMS)
RNFS_VERSION=0.1.5
RNFS_WHEEL_SHA256=419a141e7c03086ffc3f40ebf44f6cd7dbea5838c823070689781f7485a4550f
RNFS_WHEEL=https://github.com/afit21/rnfs/releases/download/v$RNFS_VERSION/rnfs-$RNFS_VERSION-py3-none-any.whl

# Checks

python_ok() { [ "$("$1" -I -c 'import sys; print(sys.version_info >= (3, 11))' 2>/dev/null)" = True ]; }

find_python() {
    if [ -n "$PYTHON" ]; then
        command -v "$PYTHON" >/dev/null 2>&1 || die "Python not found: $PYTHON"
        python_ok "$PYTHON" || die "$PYTHON is older than Python 3.11, which the gateway needs"
        return
    fi
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then PYTHON=$candidate; return; fi
    done
    die "the gateway needs Python 3.11 or newer with venv (e.g. sudo apt install python3 python3-venv), or name
one with --python /path/to/python3.X"
}

check_venv_module() {  # Debian and Ubuntu ship venv's pip bootstrap separately
    probe=$(mktemp -d "${TMPDIR:-/tmp}/gateway-venv-check.XXXXXX")
    if ! "$PYTHON" -I -m venv "$probe/venv" >/dev/null 2>&1; then
        rm -rf "$probe"
        die "$PYTHON can't create virtual environments. Install venv (e.g. sudo apt install python3-venv), then run this again"
    fi
    rm -rf "$probe"
}

ours() { [ -L "$1" ] && [ "$(readlink "$1")" = "$VENV/bin/rnfs-nomadnet-gateway" ]; }
unit_ours() { [ -f "$UNIT" ] && ! [ -L "$UNIT" ] && grep -qF "$UNIT_MARK" "$UNIT"; }
have_user_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }
system_unit_exists() {
    command -v systemctl >/dev/null 2>&1 && systemctl --system cat "$UNIT_NAME" >/dev/null 2>&1
}

check_paths() {  # fail early if the command or the unit is someone else's (unless --force)
    if { [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; } && ! ours "$LAUNCHER" && [ "$FORCE" = no ]; then
        die "$LAUNCHER already exists and wasn't installed by this script (pipx?).
Remove it (e.g. pipx uninstall rnfs-nomadnet-gateway) or run this again with --force to replace it."
    fi
    if [ "$SERVICE" = yes ] && { [ -e "$UNIT" ] || [ -L "$UNIT" ]; } && ! unit_ours && [ "$FORCE" = no ]; then
        die "$UNIT already exists and wasn't written by this script.
Stop and remove it (systemctl --user disable --now $UNIT_NAME), or run this again with --force to replace it."
    fi
}

# What runs on this machine: the rnfs daemon (whose user, and which --dir), rnsd, and gateways. One line each:
# kind, uid, user, pid, and for this user's rnfs daemon its directory (written to the settings as rnfs_dir)
scan_processes() {
    "$PYTHON" -I - <<'EOF'
import os, pwd

def program(args, names, module):
    """Where the program named by names (or `-m module`) is in a command line, or None."""
    for i, arg in enumerate(args[:3]):
        if os.path.basename(arg) in names:
            return i
    for i in range(len(args) - 1):
        if args[i] == "-m" and args[i + 1] == module:
            return i + 1
    return None

def rnfs_dir(args, cwd):
    value = None
    for i, arg in enumerate(args):
        if arg == "--dir" and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--dir="):
            value = arg.split("=", 1)[1]
    if not value:
        return os.path.expanduser("~/.rnfs")
    path = os.path.expanduser(value)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))

me = os.getuid()
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        uid = os.stat(f"/proc/{pid}").st_uid
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        continue
    kind = None
    found = program(args, ("rnfs", "rnfs.py"), "rnfs")
    if found is not None and "daemon" in args[found + 1:]:
        kind = "rnfs"
    elif program(args, ("rnsd",), "RNS.Utilities.rnsd") is not None:
        kind = "rnsd"
    elif program(args, ("rnfs-nomadnet-gateway",), "rnfs_nomadnet_gateway") is not None:
        kind = "gateway"
    if kind is None or any(c in "\t\n" for c in "".join(args)):
        continue
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    directory = ""
    if kind == "rnfs" and uid == me:
        try:
            directory = rnfs_dir(args, os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            pass
    print(kind, uid, user, pid, directory, sep="\t")
EOF
}

# Settings: config.toml made from the gateway's own example if there is none, then the given settings written
# in (replacing those lines; --allow adds to the identities already allowed), checked by the gateway's own
# validation before anything is replaced. The old file is kept as config.toml.bak
write_settings() {  # write_settings DETECTED_RNFS_DIR
    "$VENV/bin/python" -I - "$GATEWAY_DIR" "$NAME" "$ACCESS" "$ALLOW" "$RNFS_DIR_GIVEN" "$1" <<'EOF'
import json, os, re, shutil, sys, tomllib
from rnfs_nomadnet_gateway.config import DEFAULT_RNFS_DIR, Config, write_example

gateway_dir, name, access, allow, rnfs_dir_given, detected = sys.argv[1:7]
os.makedirs(gateway_dir, mode=0o700, exist_ok=True)
os.chmod(gateway_dir, 0o700)
path = os.path.join(gateway_dir, "config.toml")
existed = os.path.exists(path)
write_example(gateway_dir)
with open(path, encoding="utf-8") as f:
    text = f.read()
try:
    current = tomllib.loads(text)
except tomllib.TOMLDecodeError as e:
    sys.exit(f"can't read {path}: {e}. Fix or remove it, then run this again")

settings = {}
if name:
    settings["node_name"] = name
if access:
    settings["access"] = access
if allow:
    allowed = [h.strip().strip("<>").lower() for h in current.get("allowed_identities", []) if isinstance(h, str)]
    for h in allow.split(","):
        if h and h.strip("<>").lower() not in allowed:
            allowed.append(h.strip("<>").lower())
    settings["allowed_identities"] = allowed
configured = current.get("rnfs_dir")
if rnfs_dir_given:
    settings["rnfs_dir"] = rnfs_dir_given
elif detected and os.path.expanduser(configured or DEFAULT_RNFS_DIR) != detected:
    # The running daemon's directory, unless the gateway would use it anyway
    settings["rnfs_dir"] = detected if detected != os.path.expanduser(DEFAULT_RNFS_DIR) else None

if not settings:
    print(path + " (unchanged)")
    sys.exit(0)

# Drop the uncommented lines setting these (an array may span lines), then add them at the end
MARK = "# Set by install.sh (run it again to change them, or edit them here)"
lines, skipping = [], False
for line in text.splitlines():
    if skipping:
        skipping = "]" not in line
        continue
    match = re.match(r"\s*([A-Za-z_]+)\s*=\s*(.*)", line)
    if match and match.group(1) in settings:
        skipping = match.group(2).startswith("[") and "]" not in match.group(2)
        continue
    if line != MARK:
        lines.append(line)
while lines and not lines[-1].strip():
    lines.pop()
lines += ["", MARK]
lines += [f"{key} = {json.dumps(value)}" for key, value in settings.items() if value is not None]

temp = path + ".new"
fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
check_dir = os.path.join(gateway_dir, ".install-check")
shutil.rmtree(check_dir, ignore_errors=True)
os.mkdir(check_dir, 0o700)
os.replace(temp, os.path.join(check_dir, "config.toml"))
try:
    Config.load(check_dir)
except (ValueError, TypeError) as e:
    shutil.rmtree(check_dir)
    sys.exit(f"those settings aren't valid: {e}")
if existed:
    shutil.copy2(path, path + ".bak")
os.replace(os.path.join(check_dir, "config.toml"), path)
os.rmdir(check_dir)
print(path + (" (the old one is config.toml.bak)" if existed else ""))
EOF
}

# The code

install_code() {
    [ -n "$SOURCE" ] || die "nothing to install: run this script from the gateway's source directory, or set GATEWAY_SOURCE"
    if [ -d "$SOURCE" ]; then
        [ -f "$SOURCE/pyproject.toml" ] && [ -d "$SOURCE/rnfs_nomadnet_gateway" ] ||
            die "$SOURCE isn't the gateway's source directory"
    fi
    if ! { [ -x "$VENV/bin/python" ] && python_ok "$VENV/bin/python"; }; then
        check_venv_module
        rm -rf "$VENV"
        mkdir -p "$PREFIX"
        say "Creating a venv in $VENV"
        "$PYTHON" -I -m venv "$VENV" || die "couldn't create a venv with $PYTHON"
    fi
    work=$(mktemp -d "${TMPDIR:-/tmp}/gateway-install.XXXXXX")
    trap 'rm -rf "$work"' EXIT INT TERM
    build=$SOURCE
    if [ -d "$SOURCE" ]; then  # built from a copy, so the source gets no build/ or *.egg-info
        build=$work/src
        mkdir "$build"
        cp -R "$SOURCE/pyproject.toml" "$SOURCE/README.md" "$SOURCE/rnfs_nomadnet_gateway" "$build/"
        find "$build" -name __pycache__ -type d -prune -exec rm -rf {} +
    fi
    if [ "$("$VENV/bin/python" -I -c 'import rnfs; print(rnfs.__version__)' 2>/dev/null)" != "$RNFS_VERSION" ]; then
        say "Installing rnfs $RNFS_VERSION's client library from its GitHub release"
        # pip checks the wheel against the hash after # before installing it
        # shellcheck disable=SC2086
        "$VENV/bin/python" -I -m pip $PIP_OPTS install --quiet "rnfs @ $RNFS_WHEEL#sha256=$RNFS_WHEEL_SHA256" ||
            die "pip couldn't install rnfs $RNFS_VERSION from $RNFS_WHEEL (are GitHub and PyPI reachable?)"
    fi
    say "Installing the gateway from $SOURCE (and Reticulum from PyPI)"
    # shellcheck disable=SC2086
    "$VENV/bin/python" -I -m pip $PIP_OPTS wheel --quiet --no-deps --wheel-dir "$work" "$build" ||
        die "couldn't build the gateway from $SOURCE"
    wheel=""
    for found in "$work"/rnfs_nomadnet_gateway-*.whl; do [ -f "$found" ] && wheel=$found; done
    [ -n "$wheel" ] || die "building $SOURCE made no wheel"
    # Its dependencies, then the gateway itself again: its version number doesn't change with every edit
    # shellcheck disable=SC2086
    "$VENV/bin/python" -I -m pip $PIP_OPTS install --quiet "$wheel" ||
        die "pip couldn't install the gateway's dependencies (is PyPI reachable?)"
    # shellcheck disable=SC2086
    "$VENV/bin/python" -I -m pip $PIP_OPTS install --quiet --force-reinstall --no-deps "$wheel" ||
        die "pip couldn't install the gateway"
    rm -rf "$work"
    trap - EXIT INT TERM
    mkdir -p "$BIN_DIR"
    if ! ours "$LAUNCHER"; then rm -f "$LAUNCHER"; fi
    ln -sf "$VENV/bin/rnfs-nomadnet-gateway" "$LAUNCHER"
    "$LAUNCHER" --version >/dev/null 2>&1 || die "the installed gateway doesn't run: $LAUNCHER --version failed"
}

write_unit() {  # write_unit WANTS_RNFS (yes: rnfs runs as a user service, which the gateway then starts after)
    case "$LAUNCHER" in
        *[\\\"\'[:cntrl:]]*) die "systemd can't run a command whose path holds a quote, backslash or control character: $LAUNCHER" ;;
    esac
    mkdir -p "$UNIT_DIR"
    if ! unit_ours; then rm -f "$UNIT"; fi
    wants=""
    if [ "$1" = yes ]; then wants="Wants=rnfs.service"; fi
    launcher=$(printf '%s' "$LAUNCHER" | sed -e 's/%/%%/g')
    cat > "$UNIT" <<EOF
# systemd user unit for the rnfs NomadNet gateway, $UNIT_MARK.
#   systemctl --user restart rnfs-nomadnet-gateway
#   journalctl --user -u rnfs-nomadnet-gateway -f          # logs
# To keep it running while you're logged out: loginctl enable-linger \$USER

[Unit]
Description=rnfs NomadNet gateway
# After rnsd and rnfs, so it attaches to their shared Reticulum instance instead of starting its own
After=rnsd.service rnfs.service
$wants
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart="$launcher"
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallFilter=@system-service
SystemCallArchitectures=native

[Install]
WantedBy=default.target
EOF
}

# Actions

do_install() {
    if [ "$ACTION" = install ]; then
        missing=""
        case "$ACCESS" in
            whitelist|open|"") ;;
            *) die "--access must be whitelist or open, not $ACCESS" ;;
        esac
        if ! [ -f "$GATEWAY_DIR/config.toml" ]; then  # a first install: everything must be said
            [ -n "$NAME" ] || missing="$missing --name"
            [ -n "$ACCESS" ] || missing="$missing --access"
        fi
        if [ -n "$missing" ]; then
            die "missing:$missing. For example:
  sh install.sh --name \"My rnfs gateway\" --access whitelist --allow <your identity hash>
  sh install.sh --name \"My rnfs gateway\" --access open
(sh install.sh --help for all options)"
        fi
        for hash in $(printf '%s' "$ALLOW" | tr ',' ' '); do
            printf '%s' "$hash" | tr -d '<>' | grep -Eq '^[0-9a-fA-F]{32}$' ||
                die "--allow $hash isn't an RNS identity hash (32 hex characters)"
        done
        if [ "$ACCESS" = whitelist ] && [ -z "$ALLOW" ] && [ "$ALLOW_LATER" = no ]; then  # said now: checked now
            existing=""
            if [ -f "$GATEWAY_DIR/config.toml" ]; then
                existing=$(grep -E '^[[:space:]]*allowed_identities[[:space:]]*=[[:space:]]*\[[[:space:]]*"' "$GATEWAY_DIR/config.toml" || true)
            fi
            [ -n "$existing" ] || die "--access whitelist needs at least one --allow <identity hash>: nobody could use the
gateway otherwise. Or install with --allow-later: a visitor who isn't allowed is shown their identity hash,
which they send you, and you run this again with --allow <hash>."
        fi
        if [ "$ACCESS" = open ] && { [ -n "$ALLOW" ] || [ "$ALLOW_LATER" = yes ]; }; then
            warn "--allow only matters with --access whitelist; with open, anyone can use the gateway"
        fi
    elif ! [ -f "$GATEWAY_DIR/config.toml" ]; then
        die "the gateway isn't set up here yet: install it first (sh install.sh --help)"
    fi

    find_python
    check_paths
    if [ "$(id -u)" = 0 ]; then
        warn "running as root: the gateway is installed for root, and must run as the same user as the rnfs daemon"
    fi

    # rnfs: installed, and its daemon running as this user
    processes=$(scan_processes)
    mine=$(printf '%s\n' "$processes" | awk -F '\t' -v me="$(id -u)" '$1 == "rnfs" && $2 == me')
    others=$(printf '%s\n' "$processes" | awk -F '\t' -v me="$(id -u)" '$1 == "rnfs" && $2 != me { print $3 }' | sort -u | paste -sd ' ' -)
    rnsd=$(printf '%s\n' "$processes" | awk -F '\t' '$1 == "rnsd" { print $3 }' | sort -u | paste -sd ' ' -)
    gateways=$(printf '%s\n' "$processes" | awk -F '\t' -v me="$(id -u)" '$1 == "gateway" && $2 == me { print $4 }' | tr '\n' ' ')
    rnfs_command=$(command -v rnfs 2>/dev/null || true)
    for candidate in "$HOME/.local/bin/rnfs" /usr/local/bin/rnfs /usr/bin/rnfs; do
        if [ -z "$rnfs_command" ] && [ -x "$candidate" ]; then rnfs_command=$candidate; fi
    done
    if [ -z "$mine" ] && [ -n "$others" ]; then
        die "the rnfs daemon runs as $others, not as $(id -un). The gateway reads the daemon's API token, so it must
be installed and run as that user: run this script as ${others%% *} (e.g. sudo -iu ${others%% *}, then sh $SOURCE/install.sh ...)"
    fi
    if [ -z "$mine" ] && [ -z "$rnfs_command" ] && [ -z "$RNFS_DIR_GIVEN" ]; then
        die "rnfs isn't installed here (no rnfs command, and no rnfs daemon running). Install and start it first:
  curl -fsSL https://raw.githubusercontent.com/afit21/rnfs/main/install.sh | sh -s -- --service
then run this again."
    fi
    detected=""
    if [ -n "$mine" ]; then
        # Those that have written their API token, as the gateway looks for them (a daemon just starting hasn't)
        dirs=$(printf '%s\n' "$mine" | cut -f5 | sort -u | while IFS= read -r d; do
            if [ -f "$d/api_token" ]; then printf '%s\n' "$d"; fi
        done)
        [ -n "$dirs" ] || dirs=$(printf '%s\n' "$mine" | cut -f5 | sort -u | head -n 1)
        if [ "$(printf '%s\n' "$dirs" | grep -c .)" -gt 1 ] && [ -z "$RNFS_DIR_GIVEN" ]; then
            die "more than one rnfs daemon runs as $(id -un), with these directories:
$dirs
Say which one the gateway uses with --rnfs-dir DIR."
        fi
        detected=$(printf '%s\n' "$dirs" | head -n 1)
        if [ -n "$RNFS_DIR_GIVEN" ]; then say "Using the rnfs directory $RNFS_DIR_GIVEN (--rnfs-dir)"
        else say "Found the rnfs daemon (directory $detected)"
        fi
    else
        warn "the rnfs daemon isn't running. The gateway finds it once it starts; start it before the gateway, e.g.:
  systemctl --user enable --now rnfs      (or: rnfs daemon)"
    fi
    if [ -n "$rnsd" ]; then say "Found rnsd (running as $rnsd): the gateway attaches to its shared Reticulum instance"
    elif [ -n "$mine" ]; then say "The gateway attaches to the Reticulum instance the rnfs daemon shares"
    fi

    # Another copy of the gateway would announce the same node twice
    if system_unit_exists && [ "$SERVICE" = yes ]; then
        die "a system-wide $UNIT_NAME is installed (systemctl status $UNIT_NAME): this gateway is already set up as a
system service. Update that install instead, or remove it first (sudo systemctl disable --now $UNIT_NAME), or
run this with --no-service."
    fi
    running_unit=""
    if have_user_systemd; then
        running_unit=$(systemctl --user show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)
    fi
    for pid in $gateways; do
        if [ "$pid" != "$running_unit" ] && [ "$FORCE" = no ]; then
            die "a gateway is already running as $(id -un) (process $pid), not as this script's service: two would announce
the same node. Stop it first (or its pipx/hand-made service), or run this again with --force"
        fi
    done

    install_code
    say "Installed $("$LAUNCHER" --version) as $LAUNCHER"

    # An update writes only rnfs_dir, if the running daemon's isn't the one set: the gateway doesn't look for it
    written=$(write_settings "$detected") || die "the settings weren't changed"
    say "Wrote the settings to $written"
    if check=$("$LAUNCHER" --check 2>&1); then
        say "$(printf '%s\n' "$check" | tail -n 1)"
    else
        warn "$(printf '%s\n' "$check" | tail -n 1)
The gateway starts anyway, and uses rnfs once its daemon runs."
    fi

    if [ "$SERVICE" = yes ] || unit_ours; then
        wants_rnfs=no
        if have_user_systemd && systemctl --user cat rnfs.service >/dev/null 2>&1; then wants_rnfs=yes; fi
        write_unit "$wants_rnfs"
        if have_user_systemd; then
            systemctl --user daemon-reload
            systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1
            systemctl --user restart "$UNIT_NAME"
            sleep 3
            if systemctl --user is-active --quiet "$UNIT_NAME"; then
                say "Started the gateway service (logs: journalctl --user -u rnfs-nomadnet-gateway -f)"
            else
                journalctl --user -u "$UNIT_NAME" -n 20 --no-pager >&2 2>/dev/null || true
                die "the gateway service didn't start (its log is above)"
            fi
            linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)
            if [ "$linger" != yes ]; then
                warn "the gateway stops when you log out (so does rnfs, if it runs as a user service). To keep it
running: loginctl enable-linger $(id -un)"
            fi
        else
            warn "wrote $UNIT, but there's no systemd user session here. Start it later with:
  systemctl --user daemon-reload && systemctl --user enable --now $UNIT_NAME
or run $LAUNCHER yourself"
        fi
    else
        say "Start it with: $LAUNCHER   (it runs until stopped)"
    fi

    address=$("$LAUNCHER" --print-address 2>/dev/null || true)
    say ""
    say "The gateway's address: $address"
    say "Open it in NomadNet or MeshChat: $address:/page/index.mu"
    if grep -Eq '^access = "whitelist"' "$GATEWAY_DIR/config.toml" 2>/dev/null; then
        say "Only allowed identities can use it. To allow someone: sh $SOURCE/install.sh --allow <hash>"
    fi
    case ":${PATH:-}:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR isn't on your PATH: run the gateway as $LAUNCHER, or add it to PATH in ~/.profile" ;;
    esac
}

do_uninstall() {
    if unit_ours; then
        if have_user_systemd; then systemctl --user disable --now "$UNIT_NAME" >/dev/null 2>&1 || true; fi
        rm -f "$UNIT"
        if have_user_systemd; then systemctl --user daemon-reload || true; fi
        say "Removed $UNIT"
    elif [ -e "$UNIT" ]; then
        warn "left $UNIT alone: this script didn't write it"
    fi
    if ours "$LAUNCHER"; then rm -f "$LAUNCHER"; say "Removed $LAUNCHER"
    elif [ -e "$LAUNCHER" ]; then warn "left $LAUNCHER alone: this script didn't make it"
    fi
    if [ -d "$VENV" ]; then
        rm -rf "$VENV"
        rmdir "$PREFIX" 2>/dev/null || true
        say "Removed $VENV"
    fi
    say "Kept $GATEWAY_DIR (the gateway's identity, which is its address, settings and spool); delete it yourself if you want."
}

case "$ACTION" in
    install|update) do_install ;;
    uninstall) do_uninstall ;;
esac
