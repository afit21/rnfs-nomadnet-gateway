"""The gateway's Micron pages. Pure functions of what to show; gateway.py decides what that is.

Visitors needn't know rnfs or Micron: the text is short and plain, one idea per line.
"""
import shlex

from rnfs import Entry, Info
from rnfs.fsutil import safe_default_name

from rnfs_nomadnet_gateway import micron
from rnfs_nomadnet_gateway.micron import (ACCENT, BAD, GET_HTML, GITHUB, INDEX_HTML, OK, WAIT,
                                          colour, escape, link, muted, page, target_link)
from rnfs_nomadnet_gateway.targets import Target

CODE = "dc6"  # example addresses: a literal block keeps the colour set before it



def code(intro: str, text: str) -> str:
    """A line of text, then an example of the gateway's own shown as written. A literal block: the examples hold
    backticks, and clients disagree on escaped ones (MeshChat 2.4.0 mangles an escaped link). Its colour is set
    on the line before and reset on a line after (a line of formatting alone takes no room): `= must be alone."""
    return f"{intro}`F{CODE}\n`=\n  {text}\n`=\n`f"


def label(name: str, value: str) -> str:
    """One line of a details list: the name in the accent colour, padded so values line up."""
    return f"  {colour(ACCENT, f'{name:<9}')}{value}"


def base_address(address: str | None) -> str:
    return f"{address or '<gateway address>'}:/page/index.mu"


def open_by_address(address: str | None) -> str:
    """How to open a file: by this page's address with its CID at the end."""
    return (code("Put the CID at the end of this address, then open it:", f"{base_address(address)}`cid=<CID>")
            + "\n\n" + muted("A CID can also be <cid>/<path>, or an rnfs: link. Names (name/...) don't work yet."))


def tips(address: str | None, auto_pin_max_size: int) -> str:
    """What to add to the address, and how other pages link here."""
    base = base_address(address)
    auto = f"Files up to {micron.size(auto_pin_max_size)} are ready at once.\n\n" if auto_pin_max_size else ""
    return ("Add one of these to the end of the address:\n"
            f"  {colour(ACCENT, '|o=<name>      ')}save it with this name\n"
            f"  {colour(ACCENT, '|action=view   ')}show text as a page\n"
            f"  {colour(ACCENT, '|print=1       ')}show the file itself\n"
            f"  {colour(ACCENT, '|base64=1      ')}get the file as base64 text\n\n"
            + auto
            + code("Link to a file from your own page:", f"`[Get the file`{base}`cid=<CID>]") + "\n")


def refresh(target: Target, keep: dict) -> str:
    """keep: link variables every link about this target carries on (o, the download name asked for)."""
    return "  " + target_link("Refresh", target, **keep)


def refused(identity: str | None, operator_note: str = "") -> bytes:
    """For visitors not on the whitelist: how to identify, or their identity hash to send to the operator."""
    if identity is None:
        body = ("This gateway is for allowed visitors only, and your app didn't say who you are.\n\n"
                "In NomadNet: `!Network`! → `!Known Nodes`! → this node → `!Node Info`! → turn on "
                "`!Identify When Connecting`!. Then connect again.\n\n"
                + muted("Other apps have a similar setting."))
    else:
        body = ("You're not on this gateway's list yet.\n\n"
                "Send this ID to the gateway's owner and ask to be added:\n\n"
                f"  `!{escape(identity)}`!")
    if operator_note:
        body += "\n\n" + escape(operator_note)
    return page("Access needed", body)


def index(node_name: str, identity: str | None, access: str, remaining: int, max_file_size: int,
          recent: list[str], address: str | None = None, auto_pin_max_size: int = 0,
          html_pages: bool = False) -> bytes:
    """The start page."""
    intro = ("Get files from `!rnfs`! right here in NomadNet. No rnfs app needed.\n\n"
             f"About rnfs: {colour(ACCENT, escape(GITHUB))}")
    if identity is not None:
        who = f"You: {escape(identity)}"
    elif access == "open":
        who = muted("You haven't said who you are, so you share limits with other unnamed visitors.")
    else:
        who = ""
    fetches = colour(OK if remaining else WAIT, str(remaining))
    you = "\n".join(line for line in (
        who, f"Fetches left this hour: {fetches}", f"Biggest file: {micron.size(max_file_size)}") if line)
    sections = [intro, ">>Open a file\n" + open_by_address(address), ">>You\n" + you]
    if identity is not None:
        sections.append(">>Recent\n" + ("\n".join(recent) if recent else muted("Nothing yet.")))
    sections.append(">>Tips\n" + tips(address, auto_pin_max_size))
    if html_pages:
        sections.append(muted("Also as a web page: ") + link("HTML version", INDEX_HTML))
    return page(node_name if node_name != micron.BRAND else "Welcome", *sections)


def state_colour(state: str) -> str:
    return {"ready": OK, "fetching": WAIT, "failed": BAD}.get(state, micron.MUTED)


def recent_line(link_text: str, state: str) -> str:
    """A recent fetch: its link and its state, coloured."""
    return f"  {link_text}" + (f"  {colour(state_colour(state), state)}" if state else "")


def invalid_target(message: str, address: str | None) -> bytes:
    return page("Can't open that", "That isn't a CID or rnfs link.\n" + muted(escape(message)),
                ">>Open a file\n" + open_by_address(address))


def header(target: Target) -> str:
    """The target's key, and a link to its folder when it is inside one."""
    lines = [f"  {muted(escape(target.key))}"]
    if target.parent is not None:
        lines.append("  " + target_link("↑ Folder", target.parent))
    return "\n".join(lines)


def status(code: str, text: str) -> str:
    """A status line: a coloured marker, then the text."""
    return f"  {colour(code, '●')} {text}"


def waiting(target: Target, what: str, queued: bool, keep: dict) -> bytes:
    """A lookup or listing that hasn't finished."""
    state = "Waiting in line." if queued else "This can take a while."
    return page(what.capitalize(), header(target), status(WAIT, f"{what.capitalize()}... {state}"),
                refresh(target, keep))


def busy(target: Target, keep: dict) -> bytes:
    return page("Busy", header(target), status(WAIT, "The gateway is busy. Try again in a moment."),
                refresh(target, keep))


def failure(target: Target, what: str, message: str, retry_after: str, retry: str, keep: dict,
            commands: str = "") -> bytes:
    """A failed lookup, listing or fetch, remembered for failure_ttl; retry is the action its Retry link sends.
    commands: how to get a file with rnfs (rnfs_commands)."""
    return page(f"{what} failed", header(target), status(BAD, escape(message)),
                "  " + target_link("Try again", target, action=retry, **keep) + muted(f"  (or wait {retry_after})"),
                commands)


def directory(target: Target, entries: list[Entry], page_no: int, page_size: int) -> bytes:
    """A page of a directory's entries; subdirectories link to their listing, files to their file page."""
    pages = max(1, -(-len(entries) // page_size))
    page_no = min(max(1, page_no), pages)
    shown = entries[(page_no - 1) * page_size:page_no * page_size]
    lines = []
    for entry in shown:
        try:
            child = target.child(entry.name)
        except ValueError:
            continue  # rnfs checks names; one that isn't valid can't be linked
        if entry.is_dir:
            lines.append(f"  {colour(ACCENT, '▸')} {target_link(entry.name + '/', child)}")
        else:
            executable = ", program" if entry.kind == "executable" else ""
            lines.append(f"    {target_link(entry.name, child)}  {muted(micron.size(entry.size) + executable)}")
    count = f"{len(entries):,} item{'' if len(entries) == 1 else 's'}"
    if not entries:
        lines.append(muted("  This folder is empty."))
    if pages > 1:
        count += f", page {page_no} of {pages}"
    title = f"Folder {target.names[-1]}" if target.names else "Folder"
    return page(title, header(target), "  " + muted(count), "\n".join(lines),
                "  " + navigation(target, page_no, pages) if pages > 1 else "")


def navigation(target: Target, page_no: int, pages: int, **variables: str) -> str:
    """Links to the previous and next of a target's pages (a listing's, or a text's), carrying variables."""
    links = []
    if page_no > 1:
        links.append(target_link("← Previous", target, page=str(page_no - 1), **variables))
    if page_no < pages:
        links.append(target_link("Next →", target, page=str(page_no + 1), **variables))
    return "  ".join(links)


def text_page(target: Target, text: str, page_no: int, per_page: int, variables: dict) -> str:
    """Plain text shown as written, per_page lines at a time: one page's lines, then (if there are more) which
    lines they are and links to the other pages, carrying variables (print=1 or action=view, and o)."""
    lines = text.splitlines()
    pages = max(1, -(-len(lines) // per_page))
    page_no = min(max(1, page_no), pages)
    body = micron.literal_text("\n".join(lines[(page_no - 1) * per_page:page_no * per_page]))
    if pages == 1:
        return body
    first, last = (page_no - 1) * per_page + 1, min(page_no * per_page, len(lines))
    return (body + "\n" + muted(f"Lines {first:,}-{last:,} of {len(lines):,}") + "  "
            + navigation(target, page_no, pages, **variables))


def rnfs_commands(target: Target, asked: str | None = None, sealed: bool = False, text: bool = False,
                  own_cid: str | None = None) -> str:
    """How to get the file with rnfs itself: commands to copy, the target quoted for a shell. rnfs get takes
    `<cid>/<path>`, not an rnfs: link. Empty if the target holds a character that can't be shown, so a command
    shown is always the one that works. asked: the download name asked for (o=), already checked; text: the
    file is known to be text; own_cid: the file's own CID, for a file inside a folder that the gateway's rnfs
    node has pinned on its own, so that CID alone gets it."""
    if not target.key.isprintable():
        return ""
    quoted = shlex.quote(target.key)
    saves_as = asked or (safe_default_name(target.names[-1], target.cid) if target.names else target.cid)
    get = f"rnfs get {quoted}" + (f" -o {shlex.quote(asked)}" if asked else "")
    commands = [code(f"Save it as {escape(saves_as)}:", get)]
    if own_cid:
        own = f"rnfs get {own_cid} -o {shlex.quote(saves_as)}"  # a bare CID would save under the CID
        commands.append(code("Or by the file's own CID, pinned here:", own))
    if sealed:
        commands.append(code("Or as stored, still encrypted:", f"{get} --raw"))
    if text:
        commands.append(code("Print it:", f"rnfs cat {quoted}"))
    return ">>Get it with rnfs\n" + muted("On a computer running rnfs, in the folder to save it in.") + "\n\n" + \
        "\n\n".join(commands)


def file(target: Target, info: Info, stored_size: int, action: str, download_name: str, note: str = "",
         asked: str | None = None, text: bool = False, own_cid: str | None = None) -> bytes:
    """A file's details, what can be done with it, and how to get it with rnfs. action is Micron: links, or why
    there are none. asked: the download name asked for (o=); text: the file is known to be text; own_cid: see
    rnfs_commands."""
    name = target.names[-1] if target.names else None
    details = [label("Name", escape(name))] if name else []
    details += [label("Size", micron.size(stored_size)),
               label("Saves as", escape(download_name)),
               label("CID", muted(escape(str(info.cid))))]
    if info.sealed:
        details.append(label("Note", "Encrypted. It downloads still encrypted."))
    if info.executable:
        details.append(label("Note", "A program (marked executable)."))
    title = f"File {name}" if name else "File"
    note = f"  {colour(WAIT, escape(note))}" if note else ""
    return page(title, header(target), "\n".join(details), note, f"  {action}",
                rnfs_commands(target, asked, info.sealed, text, own_cid))


def refusal(text: str) -> str:
    """Why a file can't be fetched now, in place of its links."""
    return colour(WAIT, escape(text))


def fetch_link(target: Target, keep: dict) -> str:
    return f"`!{target_link('Fetch', target, action='fetch', **keep)}`!" + muted("  copies it here first")


def view_link(target: Target, keep: dict, html: bool = False) -> str:
    """Show the file as a page: Micron or text through the gateway, or an HTML file on the HTML path."""
    if html:
        return link("View", GET_HTML, target=target.link_value(), print="1", **keep)
    return target_link("View", target, action="view", **keep)


def view(target: Target, name: str, text: str, micron_page: bool, download_path: str, keep: dict, page_no: int = 1,
         per_page: int = 50) -> bytes:
    """A file's content as a page: Micron (made safe to embed) for a .mu file, else text as written, per_page
    lines at a time (page_no)."""
    top = (muted(f"Showing {escape(name)}  ·  ") + download_link(download_path, "Download") + muted("  ·  ")
           + target_link("Details", target, **keep))
    if micron_page:
        body = micron.hosted_micron(text)
    else:
        body = text_page(target, text, page_no, per_page, {"action": "view", **keep})
    # `` resets any formatting the content left on, < any section depth
    return "\n".join([micron.PAGE_HEADER, micron.NO_COLOURS, micron.banner(), "", top, "-", body, "``", "<",
                      micron.footer(), ""]).encode()


def download_link(path: str, text: str) -> str:
    """A link to a file's own download path, which carries no variables."""
    return link(text, path)


def file_links(path: str, download_name: str, view_text: str = "") -> str:
    """A ready file's links: Download, and View when it can be shown."""
    return f"`!{download_link(path, f'Download {download_name}')}`!" + (f"  {view_text}" if view_text else "")


def fetching(target: Target, elapsed: str, queued: bool, keep: dict, commands: str = "") -> bytes:
    """A fetch that hasn't finished. commands: how to get the file with rnfs (rnfs_commands)."""
    state = "Waiting in line." if queued else f"Started {elapsed} ago. Big files can take hours."
    return page("Fetching", header(target), status(WAIT, f"Fetching... {state}"), refresh(target, keep), commands)


def not_ready(target: Target, keep: dict) -> bytes:
    """A /file/get request for a file that isn't in the spool: displayed as a page."""
    return page("Not ready", header(target), status(WAIT, "Not here yet, or removed since."),
                "  " + target_link("Open its page", target, **keep))
