"""Building Micron pages safely.

Names and text from DAGs, the daemon and visitors are untrusted: escape() removes control characters (and every
other non-printable one, such as bidi overrides) and escapes Micron's formatting characters, as the existing
companion pages do. Untrusted text is never put at the start of a line, where Micron reads >, <, # and - as
headings, comments and dividers.
"""
from rnfs_nomadnet_gateway.targets import Target

PAGE_HEADER = "#!c=0"  # never cache: every page shows live state
GET_PAGE = "/page/get.mu"
INDEX_PAGE = "/page/index.mu"
FILE_PATH = "/file/get"
# The same pages as HTML (webpage.py), for clients that render a .html page as HTML (MeshChatX)
GET_HTML = "/page/get.html"
INDEX_HTML = "/page/index.html"
DIRECTIVE = "#!"
AUTO_LOADING = ("`{", "`(")  # partials and images: hosted Micron never keeps them
GITHUB = "https://github.com/afit21/rnfs/"
BRAND = "rnfs gateway"

# Colours (3 hex digits), chosen to read on NomadNet's and MeshChat's dark and light backgrounds
BAR_BG, BAR_FG = "246", "fff"  # the banner
ACCENT = "4ae"  # labels, the GitHub link
MUTED = "888"  # details and hints
OK = "3b6"  # ready
WAIT = "e90"  # working, or a limit
BAD = "e55"  # failed
# NomadNet takes a page's colours from the first #!bg= and #!fg= anywhere in it, even mid-line in a literal block;
# empty ones first, which it ignores, keep hosted text and names (a file named x#!bg=fff) from setting them
NO_COLOURS = "#!bg=\n#!fg="


def escape(text) -> str:
    """text with non-printable characters removed and ` and \\ escaped, so it shows literally."""
    text = "".join(c for c in str(text) if c.isprintable())
    return text.replace("\\", "\\\\").replace("`", "\\`")


def link(label: str, path: str, **variables: str) -> str:
    """A link on this node: label is escaped; path and variables must not hold ` | = ] (Target.link_value
    and plain words never do)."""
    for part in (path, *variables.keys(), *variables.values()):
        if any(c in part for c in "`]|=\n") or not part.isprintable():
            raise ValueError("a link can't hold ` ] | = or non-printable characters")
    fields = "|".join(f"{name}={value}" for name, value in variables.items())
    # A label is split on ` and ended by ] whatever escapes it holds, and shown without unescaping
    label = "".join(c for c in str(label) if c.isprintable()).replace("`", "'").replace("]", ")")
    return f"`[{label}`:{path}`{fields}]" if fields else f"`[{label}`:{path}]"


def target_link(label: str, target: Target, **variables: str) -> str:
    """A link to the target's page."""
    return link(label, GET_PAGE, target=target.link_value(), **variables)


def file_link(label: str, target: Target) -> str:
    """A download link: NomadNet treats paths starting with /file/ as downloads."""
    return link(label, FILE_PATH, target=target.link_value())


def size(num_bytes: int) -> str:
    """A human-readable size, e.g. "3.2 MiB"."""
    value = float(num_bytes)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "bytes":
                return "1 byte" if num_bytes == 1 else f"{num_bytes:,} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} bytes"


def duration(seconds: float) -> str:
    """A rough duration, e.g. "5 minutes"."""
    seconds = max(0, int(round(seconds)))
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= length:
            count = round(seconds / length)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def colour(code: str, text: str) -> str:
    """text (Micron) in a colour, then back to the default."""
    return f"`F{code}{text}`f"


def muted(text: str) -> str:
    return colour(MUTED, text)


def banner() -> str:
    """The bar at the top of every page. Clients align a line as set at its end, so the alignment is reset on
    the next line (a line of formatting alone takes no room)."""
    return f"`c`B{BAR_BG}`F{BAR_FG}`!  {BRAND}  `!`f`b\n`a"


def footer() -> str:
    """The end of every page: a link home and the project's GitHub (plain text: NomadNet can't open web
    addresses, it reads them as node addresses)."""
    return f"-\n`c{link('Home', INDEX_PAGE)}{muted('  ·  rnfs: ')}{colour(ACCENT, GITHUB)}\n`a"


def page(title: str, *sections: str) -> bytes:
    """A whole page: the no-cache header, the banner, a heading, the sections, and the footer."""
    body = "\n\n".join(section.rstrip("\n") for section in sections if section)
    return f"{PAGE_HEADER}\n{NO_COLOURS}\n{banner()}\n\n>{escape(title)}\n\n{body}\n\n{footer()}\n".encode("utf-8")


def _clean_line(line: str) -> str:
    """A line of hosted text without control characters (tabs kept), which a terminal client could act on."""
    return "".join(c for c in line if c == "\t" or c.isprintable())


def hosted_micron(text: str, directives: bool = False) -> str:
    """Micron from rnfs content, made safe to show through the gateway: partials and images dropped, which a
    client loads by itself from wherever they point, and page directives (#!…) unless directives (the content
    shown as a page of its own); a literal block or table left open is closed, so lines after it are read as
    markup.

    NomadNet reads a line's markup again after a leading < (section reset), \\ (escape) or > (a heading holding
    a field), so lines are judged without those: `<<`{…}` is a partial too. A table toggle is written without
    them, so whether a table is left open is known."""
    lines, literal, table = [], False, False
    for line in text.splitlines():
        line = _clean_line(line)
        if line == "`=":
            literal = not literal
        elif not literal:
            bare = line.lstrip("<>\\")
            if bare.startswith(AUTO_LOADING) or (bare.startswith(DIRECTIVE) and not (directives and bare == line)):
                continue
            if bare.startswith("`t"):
                line, table = bare, not table
        lines.append(line)
    if literal:
        lines.append("`=")
    if table:
        lines.append("`t")
    return "\n".join(lines)


def without_auto_loading(text: str) -> str:
    """Other content (HTML) shown as the page itself, as hosted_micron sees it: without control characters, or
    lines a Micron client would read as a partial or an image (if one renders it as Micron)."""
    lines = (_clean_line(line) for line in text.splitlines())
    return "\n".join(line for line in lines if not line.lstrip("<>\\").startswith(AUTO_LOADING))


def literal_text(text: str) -> str:
    """Plain text shown as written: a literal block, its own `= lines escaped."""
    lines = ["\\`=" if line == "`=" else line for line in map(_clean_line, text.splitlines())]
    return "\n".join(["`=", *lines, "`="])
