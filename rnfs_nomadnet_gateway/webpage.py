"""The gateway's pages as HTML, for clients that render a page whose path ends in .html as HTML (MeshChatX).

Every page is built once, as Micron (pages.py); to_html converts it, so the two versions never drift apart. It
handles the Micron the gateway writes and degrades gracefully on the rest, since hosted .mu files are converted
too: every piece of text is HTML-escaped, colours must be hex digits, and links keep only node paths (the
client turns `/page/…` and `:/page/…` hrefs into links on this node and drops anything else). Fields can't be
sent from an HTML page, so a paragraph holding one (a hosted page's form) is left out.
"""
import html
import re

from rnfs_nomadnet_gateway.micron import BAR_BG, GET_HTML, GET_PAGE, INDEX_HTML, INDEX_PAGE

HTML_PATHS = {INDEX_PAGE: INDEX_HTML, GET_PAGE: GET_HTML}
_HEX = re.compile(r"[0-9a-fA-F]{3}")
_NODE_LINK = re.compile(r"(?:[0-9a-fA-F]{32})?:/\S*|/(?:page|file)/\S*")

STYLE = """
body { margin: 0; background: #12161c; color: #dfe4ea; font: 15px/1.55 system-ui, sans-serif; }
main { max-width: 46rem; margin: 0 auto; padding: 1.25rem 1rem 2rem; }
h1 { font-size: 1.45rem; margin: .9rem 0 .6rem; }
h2 { font-size: 1.05rem; margin: 1.6rem 0 .5rem; padding-bottom: .25rem; color: #8fc9ff;
     border-bottom: 1px solid #2a323d; }
h3 { font-size: 1rem; margin: 1.2rem 0 .4rem; }
a { color: #6cb8ff; text-decoration: none; border-bottom: 1px solid #2f5f8f; }
hr { border: 0; border-top: 1px solid #2a323d; margin: 1.4rem 0 .8rem; }
pre { background: #0b0e12; border: 1px solid #222a33; border-radius: 6px; padding: .6rem .8rem;
      white-space: pre-wrap; word-break: break-all; font: 13px/1.5 ui-monospace, monospace; }
.l { white-space: pre-wrap; overflow-wrap: anywhere; }
.l:empty { height: .7em; }
.i { font: 13.5px/1.6 ui-monospace, "DejaVu Sans Mono", monospace; }
.c { text-align: center; } .r { text-align: right; }
.banner { display: inline-block; padding: .2rem .9rem; border-radius: 999px; font-weight: 600; }
"""


class _Style:
    def __init__(self):
        self.reset()
        self.align = ""

    def reset(self):
        self.fg = self.bg = None
        self.bold = self.underline = self.italic = False

    def css(self) -> str:
        parts = []
        if self.fg:
            parts.append(f"color:#{self.fg}")
        if self.bg:
            parts.append(f"background:#{self.bg}")
        if self.bold:
            parts.append("font-weight:600")
        if self.underline:
            parts.append("text-decoration:underline")
        if self.italic:
            parts.append("font-style:italic")
        return ";".join(parts)


def _href(url: str, fields: str) -> str | None:
    """A link's href, or None if it can't be followed from HTML: not a node path, or it sends form fields."""
    url = url.strip()
    if not _NODE_LINK.fullmatch(url):
        return None
    variables = [f for f in fields.split("|") if f] if fields else []
    if any("=" not in f for f in variables):
        return None
    node, sep, path = ("", "", url) if url.startswith("/") else url.partition(":")
    path = HTML_PATHS.get(path, path)  # the gateway's own pages stay HTML
    url = f"{node}{sep}{path}"
    return url + ("`" + "|".join(variables) if variables else "")


def _span(style: _Style, body: str) -> str:
    """body (HTML) in the style; the gateway's banner colour gets the banner's look."""
    css = style.css()
    if not css:
        return body
    banner = ' class="banner"' if style.bg == BAR_BG else ""
    return f'<span{banner} style="{css}">{body}</span>'


def _inline(text: str, style: _Style, escaped_first: bool = False) -> tuple[str, str]:
    """One line's text and inline markup as HTML, and its alignment: as it was where its text starts (Micron
    pages set it at the start of a line and reset it at the end)."""
    out: list[str] = []
    run: list[str] = []
    align: list[str] = []

    def flush():
        if run:
            align.append(style.align)
            out.append(_span(style, html.escape("".join(run))))
            run.clear()

    i, n = 0, len(text)
    if escaped_first and n:
        run.append(text[0])
        i = 1
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            run.append(text[i + 1])
            i += 2
            continue
        if c != "`" or i + 1 >= n:
            run.append(c)
            i += 1
            continue
        command = text[i + 1]
        i += 2
        if command == "`":
            flush()
            style.reset()
            style.align = ""
        elif command in "!_*":
            flush()
            attribute = {"!": "bold", "_": "underline", "*": "italic"}[command]
            setattr(style, attribute, not getattr(style, attribute))
        elif command in "FB":
            flush()
            if text[i:i + 1] == "T":  # true colour, `FTrrggbb
                value, i = text[i + 1:i + 7], i + 7
                ok = re.fullmatch(r"[0-9a-fA-F]{6}", value)
            else:
                value, i = text[i:i + 3], i + 3
                ok = _HEX.fullmatch(value)
            if ok:
                setattr(style, "fg" if command == "F" else "bg", value)
        elif command in "fb":
            flush()
            setattr(style, "fg" if command == "f" else "bg", None)
        elif command in "clr":
            style.align = command
        elif command == "a":
            style.align = ""
        elif command == "[":
            end = text.find("]", i)
            end = n if end < 0 else end
            parts = text[i:end].split("`")
            i = end + 1
            label, url, fields = (parts + ["", ""])[:3] if len(parts) > 1 else (parts[0], parts[0], "")
            flush()
            align.append(style.align)
            href = _href(url, fields)
            body = _span(style, html.escape(label))
            out.append(f'<a href="{html.escape(href, quote=True)}">{body}</a>' if href else body)
        elif command == "<":
            tick = text.find("`", i)  # searched, not sliced: a line of many fields stays linear
            end = text.find(">", tick + 1 if tick >= 0 else i)
            i = n if end < 0 else end + 1
        elif command == "{":
            end = text.find("}", i)
            i = n if end < 0 else end + 1
    flush()
    return "".join(out), (align[0] if align else style.align)


def to_html(markup: str) -> bytes:
    """A Micron page as a whole HTML document."""
    style = _Style()
    body: list[str] = []
    literal: list[str] | None = None
    title = ""
    paragraph = 0  # where the current paragraph starts in body
    skipping = False  # the rest of a paragraph holding a field
    for line in markup.split("\n"):
        if line == "`=":
            if literal is None:
                literal = []
            else:
                body.append("<pre>" + html.escape("\n".join(literal)) + "</pre>")
                literal = None
            continue
        if literal is not None:
            literal.append(line)
            continue
        if not line:
            skipping, paragraph = False, len(body) + 1
        elif skipping:
            continue
        if line.startswith("#"):
            continue  # comments and page directives
        while line.startswith("<"):
            line = line[1:]
        if "`<" in line:  # a field can't be sent from HTML: its paragraph (e.g. its prompt) is left out
            del body[paragraph:]
            skipping = True
            continue
        heading = len(line) - len(line.lstrip(">"))
        if heading:
            text, _ = _inline(line[heading:], style)
            title = title or html.unescape(re.sub(r"<[^>]+>", "", text))
            level = min(heading, 3)
            body.append(f"<h{level}>{text}</h{level}>")
            paragraph = len(body)
            continue
        if line in ("-",) or (line.startswith("-") and len(line) == 2):
            body.append("<hr>")
            continue
        escaped = line.startswith("\\")
        text, align = _inline(line[1:] if escaped else line, style, escaped_first=escaped)
        cls = {"c": "l c", "r": "l r"}.get(align, "l") + (" i" if line.startswith("  ") else "")
        body.append(f'<div class="{cls}">{text}</div>')
    if literal is not None:
        body.append("<pre>" + html.escape("\n".join(literal)) + "</pre>")
    head = (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title or 'rnfs gateway')}</title><style>{STYLE}</style></head>\n")
    return (head + "<body><main>\n" + "\n".join(body) + "\n</main></body></html>\n").encode("utf-8")


def text_page(text: str, title: str) -> bytes:
    """Plain text as an HTML page, shown as written."""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{STYLE}</style></head>\n<body><main><pre>{html.escape(text)}</pre></main></body></html>\n"
            ).encode("utf-8")
