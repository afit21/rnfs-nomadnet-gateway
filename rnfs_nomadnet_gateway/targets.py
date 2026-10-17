"""Targets (specification section 5): `<cid>[/path]` or an `rnfs:` URI (rnfs §13) naming one.

Parsed and checked by rnfs's own rules (rnfs.features.lxmf and rnfs.content.dag, neither of which loads
anything more), so nothing that rnfs would refuse, nor nicks or names, reaches its API.
"""
from dataclasses import dataclass

from rnfs import Cid
from rnfs.content.dag import check_name
from rnfs.features import lxmf


@dataclass(frozen=True)
class Target:
    """A root CID and the path names below it."""

    cid: str
    names: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """The normalized form, `<cid>[/name…]`, which the API takes and states are keyed by."""
        return "/".join((self.cid, *self.names))

    def uri(self) -> str:
        """The canonical rnfs: URI: every byte other than ASCII letters, digits and -._~ percent-encoded."""
        return lxmf.Link(Cid.parse(self.cid), path=self.names).uri()

    def link_value(self) -> str:
        """The target as a Micron link variable: a bare CID, or the URI, which holds no character NomadNet
        reads as a link's syntax (` | = ] and spaces are all percent-encoded)."""
        return self.uri() if self.names else self.cid

    def child(self, name: str) -> "Target":
        """The target of an entry name in this directory. Raises ValueError for a name rnfs wouldn't allow."""
        check_name(name)
        return Target(self.cid, (*self.names, name))

    @property
    def parent(self) -> "Target | None":
        """The directory holding this target, or None for a root."""
        return Target(self.cid, self.names[:-1]) if self.names else None


def parse_target(text) -> Target:
    """A target from what a visitor typed or a link carried: `<cid>[/path]` (one trailing slash allowed) or an
    rnfs: URI, whose `from` hint is ignored. Surrounding whitespace is ignored. Raises ValueError for anything
    else, and for `name/` targets, which the gateway doesn't follow yet."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no target was given: enter a CID, <cid>/<path> or an rnfs: link")
    link = lxmf.parse_target(text.strip())
    if link.name is not None:
        raise ValueError("name/ targets aren't supported by this gateway yet")
    return Target(str(link.cid), link.path)
