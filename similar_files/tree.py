"""Groups as one ``.xft`` tree file — XeFM's format for a tree of references.

The other output, beside a folder of ``.m3u8`` playlists (:mod:`.playlist`).
One file holds every group: a group is a node whose children are the files,
each a reference to a place with the attributes recorded when it was read
(size, modification time) and the similarity to its anchor. XeFM browses it
the way it browses an archive.

The format is XeFM's and its specification lives there (``doc/XFT_FORMAT.md``
in the XeFM repository, discussion xefm#549). This module only writes it; it
imports nothing from XeFM.

A scan can write the file many times. While it runs the file is a
**snapshot**: ``final`` is false, and the groups in it may still change —
for similar files a later file can join a group or become a new anchor. The
last write sets ``final``. Each write replaces the file whole (a temporary
file, then :func:`os.replace`), and ``generation`` counts the writes, so a
reader can tell a new snapshot from the one it has.
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .model import Group
from .playlist import _basename, playlist_name

FORMAT = "xft"
VERSION = 1
SUFFIX = ".xft"


def _unique(name: str, taken: set[str]) -> str:
    """``name``, or ``name [2]``, ``name [3]``… — unique within one parent."""
    key = unicodedata.normalize("NFC", name).casefold()
    if key not in taken:
        taken.add(key)
        return name
    n = 2
    while True:
        candidate = f"{name} [{n}]"
        ckey = unicodedata.normalize("NFC", candidate).casefold()
        if ckey not in taken:
            taken.add(ckey)
            return candidate
        n += 1


def _leaf(item, role: str, similarity: float, taken: set[str]) -> dict:
    return {
        "name": _unique(_basename(item.uri), taken),
        "target": item.uri,
        "attrs": {"size": int(item.size), "mtime": item.mtime_ns / 1e9},
        "extra": {"role": role, "similarity": round(float(similarity), 4)},
    }


def group_node(group: Group, taken: set[str], *, final: bool = False) -> dict:
    """One group as a node: the anchor, then the members nearest first."""
    leaves: set[str] = set()
    children = [_leaf(group.anchor, "anchor", 1.0, leaves)]
    children += [_leaf(m.item, "member", m.similarity, leaves) for m in group.members]
    node = {
        "name": _unique(playlist_name(group), taken),
        "extra": {"method": group.method, "version": group.version, "params": group.params,
                  "threshold": group.threshold, "members": len(group.members)},
        "children": children,
    }
    if final:
        node["final"] = True
    return node


def render(groups: Iterable[Group], *, title: str, final: bool, generation: int,
           done: int = 0, total: int = 0, final_groups: Optional[set] = None,
           complete: bool = True) -> dict:
    """The document for ``groups``. ``final_groups`` holds the ``id()`` of
    groups that can no longer change even while the scan runs (identical
    files, a size bucket at a time); every group is final once ``final`` is.
    ``complete`` is false on the last write of a scan that was stopped
    before it read everything."""
    taken: set[str] = set()
    nodes = [group_node(g, taken, final=final or (final_groups is not None and id(g) in final_groups))
             for g in groups]
    return {
        "format": FORMAT, "version": VERSION,
        "title": title,
        "generator": {"name": "similar-files", "version": __version__},
        "final": final,
        "complete": complete if final else False,
        "generation": generation,
        "written": time.time(),
        "progress": {"done": done, "total": total},
        "nodes": nodes,
    }


def write(path: str | os.PathLike, document: dict) -> Path:
    """Replace ``path`` with ``document``, whole."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(path: str | os.PathLike) -> Optional[dict]:
    """The document in ``path``, or None when it is not one this version reads."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != VERSION:
        return None
    return data


class TreeWriter:
    """Writes one scan's tree file, snapshot after snapshot. ``generation``
    counts the writes; :meth:`finish` writes the final one."""

    def __init__(self, path: str | os.PathLike, *, title: str):
        self.path = Path(path)
        self.title = title
        self.generation = 0
        #: The groups of the last snapshot, for a scan stopped before its end.
        self.last_groups: list[Group] = []
        self._last_progress = (0, 0)

    def snapshot(self, groups: list[Group], *, done: int = 0, total: int = 0,
                 final_groups: Optional[set] = None) -> None:
        self.generation += 1
        self.last_groups = list(groups)
        self._last_progress = (done, total)
        write(self.path, render(groups, title=self.title, final=False, generation=self.generation,
                                done=done, total=total, final_groups=final_groups))

    def finish(self, groups: list[Group], *, done: int = 0, total: int = 0,
               complete: bool = True) -> None:
        """The last write. A scan stopped part-way passes ``complete=False``,
        and when it found no groups of its own — a similar-file scan groups
        only at its end — the last snapshot's groups stand as its result."""
        if not complete and not groups and self.last_groups:
            groups = self.last_groups
            done, total = self._last_progress
        self.generation += 1
        write(self.path, render(groups, title=self.title, final=True, generation=self.generation,
                                done=done, total=total, complete=complete))
