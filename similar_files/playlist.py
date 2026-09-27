"""Output: one ``.m3u8`` playlist per group.

The format is a contract other programs read; it is specified in
``doc/PLAYLIST_FORMAT.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from .model import Group

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
GROUP_TAG = "#SIMILAR-FILES-GROUP:"
ITEM_TAG = "#SIMILAR-FILES-ITEM:"
SUFFIX = ".m3u8"

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
#: Room for " (123) [45].m3u8" within the common 255-byte name limit.
_MAX_STEM_BYTES = 200


def _basename(uri: str) -> str:
    name = re.split(r"[/\\]", uri.rstrip("/\\"))[-1]
    return name or "group"


def safe_name(name: str) -> str:
    """``name`` made valid as a file name on Windows, macOS and Linux."""
    name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    name = _FORBIDDEN.sub("_", name).rstrip(" .")
    if name.split(".", 1)[0].upper() in _RESERVED:
        name = "_" + name
    encoded = name.encode("utf-8")
    if len(encoded) > _MAX_STEM_BYTES:
        name = encoded[:_MAX_STEM_BYTES].decode("utf-8", "ignore").rstrip(" .")
    return name or "group"


def playlist_name(group: Group) -> str:
    """``"<anchor file name> (<member count>)"``, sanitized, without the suffix."""
    return f"{safe_name(_basename(group.anchor.uri))} ({len(group.members)})"


def _key(name: str) -> str:
    # Collisions are judged the way case-insensitive, normalizing filesystems judge them.
    return unicodedata.normalize("NFC", name).casefold()


def _writable(uri: str) -> bool:
    return "\n" not in uri and "\r" not in uri


def render(group: Group) -> str:
    """The playlist text for one group."""
    header = {
        "format": FORMAT_VERSION,
        "method": group.method,
        "version": group.version,
        "params": group.params,
        "threshold": group.threshold,
        "members": len(group.members),
    }
    lines = ["#EXTM3U", GROUP_TAG + json.dumps(header, sort_keys=True, separators=(",", ":"))]
    lines.append(ITEM_TAG + json.dumps({"role": "anchor", "similarity": 1.0}, separators=(",", ":")))
    lines.append(group.anchor.uri)
    for m in group.members:
        if not _writable(m.item.uri):
            logger.warning("%r: a line break in the name cannot be written to a playlist; left out", m.item.uri)
            continue
        info = {"role": "member", "similarity": round(m.similarity, 4)}
        lines.append(ITEM_TAG + json.dumps(info, separators=(",", ":")))
        lines.append(m.item.uri)
    return "\n".join(lines) + "\n"


def write_playlists(groups: Iterable[Group], out_dir: str | os.PathLike) -> list[Path]:
    """Write one playlist per group into ``out_dir`` (created if needed). Returns the paths written.

    Names never collide with each other or with files already in
    ``out_dir``: a clash gets `` [2]``, `` [3]``… appended. Groups whose
    anchor cannot be written (a line break in its name) are skipped and
    logged.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    taken = {_key(p.name) for p in out.iterdir()}
    written = []
    for group in groups:
        if not _writable(group.anchor.uri):
            logger.warning("%r: a line break in the name cannot be written to a playlist; group skipped", group.anchor.uri)
            continue
        stem = playlist_name(group)
        name = stem + SUFFIX
        n = 2
        while _key(name) in taken:
            name = f"{stem} [{n}]{SUFFIX}"
            n += 1
        taken.add(_key(name))
        path = out / name
        # Paths are exact bytes on POSIX; surrogateescape writes an undecodable
        # name back as the bytes it came from.
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
            f.write(render(group))
        written.append(path)
    return written


def is_our_playlist(path: str | os.PathLike) -> bool:
    """Whether ``path`` is a playlist similar-files wrote (it has the group tag)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = [f.readline() for _ in range(2)]
    except OSError:
        return False
    return head[0].strip() == "#EXTM3U" and head[1].startswith(GROUP_TAG)


def read_playlist(path: str | os.PathLike) -> tuple[dict, list[tuple[str, dict]]]:
    """Parse a playlist similar-files wrote: ``(group header, [(uri, item info), …])``, anchor first."""
    header: dict = {}
    entries: list[tuple[str, dict]] = []
    info: dict = {}
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip("\r")
            if line.startswith(GROUP_TAG):
                header = json.loads(line[len(GROUP_TAG):])
            elif line.startswith(ITEM_TAG):
                info = json.loads(line[len(ITEM_TAG):])
            elif line.startswith("#") or not line:
                continue
            else:
                entries.append((line, info))
                info = {}
    return header, entries
