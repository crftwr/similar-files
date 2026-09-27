"""Where files come from: the item protocol, and the local-path adapter.

The library never works on ``pathlib`` paths directly. Everything it scans is
an *item*: an object with the attributes of :class:`FileItem`. A local-path
adapter (:class:`LocalFile`, produced by :func:`walk`) ships here. Other
programs, such as XeFM, write their own adapter for ``ssh://`` or ``s3://``
sources and hand the items in.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterable, Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class FileItem(Protocol):
    """What the library needs to know about one file (or directory).

    ``uri``
        A stable string. It is the cache key and the line written to the
        playlist. Local files use their absolute path.
    ``size``, ``mtime_ns``
        From the listing. Together with ``uri`` they decide whether cached
        results still apply.
    ``validator``
        An optional extra validator, such as an ETag. ``None`` if the source
        has none.
    ``is_remote``
        Whether reading the content costs a network transfer. Perceptual
        layers skip remote items unless the caller opts in.
    ``open()``
        Returns a binary file object. Called only when content is needed.
    ``content_hash()``
        Returns ``"<algorithm>:<hex digest>"`` if the source can produce a
        hash without sending the bytes (``sha256sum`` on an SSH host, a
        single-part S3 ETag as ``md5:…``), or ``None``. A source that cannot
        be sure its hash is a content hash returns ``None``.
    """

    uri: str
    size: int
    mtime_ns: int
    validator: Optional[str]
    is_remote: bool

    def open(self) -> BinaryIO: ...

    def content_hash(self) -> Optional[str]: ...


@dataclass(eq=False)
class LocalFile:
    """A file on a local (or locally mounted) filesystem."""

    path: str
    size: int
    mtime_ns: int
    is_remote: bool = False
    validator: Optional[str] = None
    dev: int = 0
    ino: int = 0
    nlink: int = 1

    @property
    def uri(self) -> str:
        return self.path

    def open(self) -> BinaryIO:
        return open(self.path, "rb")

    def content_hash(self) -> Optional[str]:
        return None

    @classmethod
    def from_path(cls, path: str | os.PathLike, *, is_remote: bool = False) -> "LocalFile":
        """Make an item for one file, following a symbolic link if ``path`` is one."""
        p = os.path.abspath(os.fspath(path))
        st = os.stat(p)
        return cls._from_stat(p, st, is_remote)

    @classmethod
    def _from_stat(cls, path: str, st: os.stat_result, is_remote: bool) -> "LocalFile":
        return cls(
            path=path,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            is_remote=is_remote,
            dev=st.st_dev,
            ino=st.st_ino,
            nlink=st.st_nlink,
        )


@dataclass
class WalkStats:
    """What a walk skipped, for the caller to report."""

    files: int = 0
    hardlink_aliases: int = 0
    symlinks_skipped: int = 0
    #: Followed links to a directory already visited: a cycle, or an overlap.
    links_revisited: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)


def _is_link_like(entry: os.DirEntry) -> bool:
    """A symbolic link, or on Windows a junction or other reparse point."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _under(path: str, roots: list[str]) -> bool:
    norm = os.path.normcase(path)
    for r in roots:
        if norm == r or norm.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def walk(
    roots: Iterable[str | os.PathLike],
    *,
    follow_symlinks: bool = False,
    remote_roots: Iterable[str | os.PathLike] = (),
    stats: Optional[WalkStats] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> Iterator[LocalFile]:
    """Yield every regular file under ``roots``, each physical file once.

    - Hard links (same device and inode) are yielded once, under the first
      name found. The other names are counted in ``stats.hardlink_aliases``.
    - Symbolic links (and Windows junctions) are not followed unless
      ``follow_symlinks`` is true. When followed, each target is visited once
      and a link back to a directory already visited (a cycle) is counted
      in ``stats.links_revisited`` and not entered.
    - Overlapping roots visit each file once.
    - Where the filesystem reports no inode (``st_ino == 0``), only the
      hard-link check is skipped; the file is still yielded.
    - Items under any of ``remote_roots`` are marked ``is_remote``: network
      mounts look local, and the caller knows which ones are not.

    A root may also be a single file. Unreadable directories are logged,
    counted in ``stats.errors``, and skipped.
    """
    if stats is None:
        stats = WalkStats()
    remote = [os.path.normcase(os.path.abspath(os.fspath(r))) for r in remote_roots]
    seen_files: set[tuple[int, int]] = set()
    seen_paths: set[str] = set()
    seen_dirs: set[tuple[int, int]] = set()
    seen_dir_paths: set[str] = set()

    def error(path: str, exc: OSError) -> None:
        logger.warning("cannot read %s: %s", path, exc)
        stats.errors += 1
        stats.error_paths.append(path)

    def file_item(path: str, st: os.stat_result) -> Optional[LocalFile]:
        norm = os.path.normcase(path)
        if norm in seen_paths:
            return None
        seen_paths.add(norm)
        if st.st_ino:
            key = (st.st_dev, st.st_ino)
            if key in seen_files:
                stats.hardlink_aliases += 1
                return None
            seen_files.add(key)
        stats.files += 1
        return LocalFile._from_stat(path, st, _under(path, remote))

    def enter_dir(path: str, st: os.stat_result) -> bool:
        """Mark a directory visited; False if it was visited already."""
        if st.st_ino:
            key = (st.st_dev, st.st_ino)
            if key in seen_dirs:
                return False
            seen_dirs.add(key)
        else:
            norm = os.path.normcase(os.path.realpath(path))
            if norm in seen_dir_paths:
                return False
            seen_dir_paths.add(norm)
        return True

    for root in roots:
        root_path = os.path.abspath(os.fspath(root))
        try:
            st = os.stat(root_path)
        except OSError as exc:
            error(root_path, exc)
            continue
        if stat.S_ISREG(st.st_mode):
            item = file_item(root_path, st)
            if item is not None:
                yield item
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if not enter_dir(root_path, st):
            continue
        # Directories on the stack are already marked visited.
        stack = [root_path]
        while stack:
            if cancel is not None and cancel():
                return
            dir_path = stack.pop()
            try:
                with os.scandir(dir_path) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as exc:
                error(dir_path, exc)
                continue
            subdirs = []
            for entry in entries:
                try:
                    link = _is_link_like(entry)
                    if link and not follow_symlinks:
                        stats.symlinks_skipped += 1
                        continue
                    # A followed link's target, or the entry itself.
                    est = entry.stat(follow_symlinks=True) if link else entry.stat(follow_symlinks=False)
                except OSError as exc:
                    error(entry.path, exc)
                    continue
                if stat.S_ISDIR(est.st_mode):
                    if enter_dir(entry.path, est):
                        subdirs.append(entry.path)
                    elif link:
                        stats.links_revisited += 1
                elif stat.S_ISREG(est.st_mode):
                    item = file_item(entry.path, est)
                    if item is not None:
                        yield item
            stack.extend(reversed(subdirs))
