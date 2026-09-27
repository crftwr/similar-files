"""Layer 1: identical files, cheapest test first.

Group by size, then hash a head-and-tail sample, then hash the whole file
only for the candidates still left. The largest buckets are processed first,
so the groups that free the most space come out first.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from ._parallel import Runner
from .cache import Cache, NullCache
from .model import Cancelled, CancelCheck, Group, Member, ProgressCallback, ScanResult
from .source import FileItem

logger = logging.getLogger(__name__)

HASH_ALGORITHM = "sha256"
#: Bytes read from each end of a file for the sample hash.
SAMPLE_BYTES = 64 * 1024
_CHUNK = 1024 * 1024
_CACHE_BATCH = 256


def hash_stream(stream, stop=None) -> str:
    """``"sha256:<hex>"`` of everything ``stream`` yields. Raises :class:`Cancelled` if ``stop`` gets set."""
    h = hashlib.sha256()
    while True:
        if stop is not None and stop.is_set():
            raise Cancelled()
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        h.update(chunk)
    return f"{HASH_ALGORITHM}:{h.hexdigest()}"


def _sample_hash(item: FileItem) -> Optional[str]:
    """A hash of the file's head and tail, or ``None`` if the tail cannot be reached."""
    with item.open() as f:
        head = f.read(SAMPLE_BYTES)
        try:
            f.seek(max(item.size - SAMPLE_BYTES, 0))
        except (OSError, ValueError, AttributeError):
            return None
        tail = f.read(SAMPLE_BYTES)
    return hashlib.sha256(head + b"\0" + tail).hexdigest()


@dataclass
class _Entry:
    item: FileItem
    is_ref: bool = False
    sha: Optional[str] = None  # "sha256:…", known or computed
    other: Optional[str] = None  # a source-provided hash in another algorithm
    sample: Optional[str] = None
    from_cache: bool = False
    failed: bool = False


def _algorithm(h: str) -> str:
    return h.split(":", 1)[0].lower()


def _same_identity(a: FileItem, b: FileItem) -> bool:
    if a.uri == b.uri:
        return True
    ino_a, ino_b = getattr(a, "ino", 0), getattr(b, "ino", 0)
    return bool(ino_a and ino_a == ino_b and getattr(a, "dev", None) == getattr(b, "dev", None))


def find_identical(
    items: Iterable[FileItem],
    *,
    references: Sequence[FileItem] = (),
    cache: "Cache | NullCache | str | os.PathLike | None | bool" = None,
    min_size: int = 1,
    workers: Optional[int] = None,
    remote_workers: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[CancelCheck] = None,
    on_group: Optional[Callable[[Group], None]] = None,
) -> ScanResult:
    """Find groups of files with the same bytes.

    ``items``
        The files to scan, for example from :func:`similar_files.walk`.
    ``references``
        Reference mode: each reference that has copies among ``items``
        anchors a group of those copies. References are never members, and an
        item that *is* a reference (same URI or same inode) is not a
        candidate. Without references, the scan discovers groups among
        ``items`` and picks each anchor (the oldest file).
    ``cache``
        A :class:`Cache`, a path to one, ``None`` for the default cache, or
        ``False`` for none.
    ``min_size``
        Smaller files are ignored. The default skips empty files.
    ``on_group``
        Called with each group as soon as it is complete, largest first, on
        the calling thread.

    Every member of a group has similarity 1.0. Safe to call from a worker
    thread; ``progress`` and ``on_group`` are called on that thread.
    """
    owned, cache_obj = _open_cache(cache)
    try:
        return _find_identical(
            items, references, cache_obj, min_size, workers, remote_workers, progress, cancel, on_group
        )
    finally:
        if owned:
            cache_obj.close()


def _open_cache(cache) -> tuple[bool, "Cache | NullCache"]:
    if cache is False:
        return True, NullCache()
    if cache is None or isinstance(cache, (str, os.PathLike)):
        return True, Cache(cache)
    return False, cache


def _find_identical(items, references, cache, min_size, workers, remote_workers, progress, cancel, on_group):
    result = ScanResult()
    refs = list(references)

    # -- 1. Size buckets, from the listing alone ---------------------------
    by_size: dict[int, list[_Entry]] = defaultdict(list)
    seen: set[str] = set()
    for ref in refs:
        if ref.uri not in seen and ref.size >= min_size:
            seen.add(ref.uri)
            by_size[ref.size].append(_Entry(ref, is_ref=True))
    ref_sizes = set(by_size)
    for item in items:
        if cancel is not None and cancel():
            result.cancelled = True
            return result
        if item.uri in seen or item.size < min_size:
            continue
        if refs and (item.size not in ref_sizes or any(_same_identity(item, r) for r in refs if r.size == item.size)):
            continue
        seen.add(item.uri)
        by_size[item.size].append(_Entry(item))

    buckets = [b for b in by_size.values() if len(b) > 1]
    if refs:
        buckets = [b for b in buckets if any(e.is_ref for e in b) and any(not e.is_ref for e in b)]
    # The groups that free the most space first.
    buckets.sort(key=lambda b: (b[0].item.size * (len(b) - 1), b[0].item.size), reverse=True)

    # -- 2. Cached hashes: no reading at all -------------------------------
    touched: list[str] = []
    for bucket in buckets:
        for e in bucket:
            h = cache.content_hash(e.item.uri, e.item.size, e.item.mtime_ns, e.item.validator)
            if h is not None:
                e.sha, e.from_cache = h, True
                touched.append(e.item.uri)

    new_hashes: list[tuple] = []

    def flush(force: bool = False) -> None:
        if new_hashes and (force or len(new_hashes) >= _CACHE_BATCH):
            cache.put_hashes(new_hashes)
            new_hashes.clear()

    def fail(e: _Entry, exc: BaseException) -> None:
        e.failed = True
        logger.warning("cannot read %s: %s", e.item.uri, exc)
        result.add_error(e.item.uri)

    with Runner(workers=workers, remote_workers=remote_workers, progress=progress, cancel=cancel) as runner:
        # -- 3. Source-provided hashes and head-and-tail samples -----------
        def probe(e: _Entry) -> tuple[Optional[str], Optional[str]]:
            if runner.stop.is_set():
                raise Cancelled()
            provided = e.item.content_hash()
            if provided and _algorithm(provided) == HASH_ALGORITHM:
                return provided, None
            if e.item.size <= 2 * SAMPLE_BYTES:
                return provided, None  # A sample would read the whole file anyway.
            return provided, _sample_hash(e.item)

        probes = [(e, runner.submit(e.item.is_remote, probe, e)) for b in buckets for e in b if e.sha is None]
        runner.start_phase("sample", len(probes))
        if not runner.wait([f for _, f in probes]):
            result.cancelled = True
            return result
        for e, f in probes:
            try:
                provided, e.sample = f.result()
            except Exception as exc:  # noqa: BLE001 - one unreadable file never aborts a scan
                fail(e, exc)
                continue
            if provided and _algorithm(provided) == HASH_ALGORITHM:
                e.sha = provided
                new_hashes.append((e.item.uri, e.item.size, e.item.mtime_ns, e.item.validator, provided))
            elif provided:
                e.other = provided

        # -- 4. Decide which files need a full hash -------------------------
        plans: list[tuple[list[_Entry], Optional[str]]] = []
        to_hash: list[_Entry] = []
        for bucket in buckets:
            bucket = [e for e in bucket if not e.failed]
            if len(bucket) < 2:
                continue
            others = {_algorithm(e.other) for e in bucket if e.other}
            if len(others) == 1 and all(e.other for e in bucket):
                # Every file in the bucket came with a hash in one algorithm.
                plans.append((bucket, "other"))
                continue
            sample_counts: dict[str, int] = defaultdict(int)
            wildcard = 0  # entries whose sample is unknown: known hash, small file, or no tail
            for e in bucket:
                if e.sample is None:
                    wildcard += 1
                else:
                    sample_counts[e.sample] += 1
            for e in bucket:
                if e.sha is not None:
                    continue
                if e.sample is None or wildcard > 0 or sample_counts[e.sample] > 1:
                    to_hash.append(e)
            plans.append((bucket, None))

        # -- 5. Full hashes, largest buckets first --------------------------
        def full_hash(e: _Entry) -> str:
            if runner.stop.is_set():
                raise Cancelled()
            with e.item.open() as f:
                return hash_stream(f, runner.stop)

        futures = {id(e): runner.submit(e.item.is_remote, full_hash, e) for e in to_hash}
        runner.start_phase("hash", len(futures))
        for bucket, mode in plans:
            pending = [futures[id(e)] for e in bucket if id(e) in futures]
            if not runner.wait(pending):
                result.cancelled = True
                break
            for e in bucket:
                f = futures.get(id(e))
                if f is None:
                    continue
                try:
                    e.sha = f.result()
                except Exception as exc:  # noqa: BLE001
                    fail(e, exc)
                    continue
                new_hashes.append((e.item.uri, e.item.size, e.item.mtime_ns, e.item.validator, e.sha))
            flush()
            for group in _make_groups(bucket, mode, bool(refs)):
                result.groups.append(group)
                if on_group is not None:
                    on_group(group)

        # Everything finished so far is valid, cancelled or not.
        flush(force=True)
        cache.touch(uris=touched)
    return result


def _make_groups(bucket: list[_Entry], mode: Optional[str], reference_mode: bool) -> list[Group]:
    by_hash: dict[str, list[_Entry]] = defaultdict(list)
    for e in bucket:
        key = e.other if mode == "other" else e.sha
        if key is not None and not e.failed:
            by_hash[key].append(e)
    groups = []
    for same in by_hash.values():
        if len(same) < 2:
            continue
        if reference_mode:
            copies = sorted((e.item for e in same if not e.is_ref), key=lambda i: i.uri)
            for ref in (e.item for e in same if e.is_ref):
                if copies:
                    groups.append(_group(ref, copies))
        else:
            ordered = sorted((e.item for e in same), key=lambda i: (i.mtime_ns, i.uri))
            groups.append(_group(ordered[0], sorted(ordered[1:], key=lambda i: i.uri)))
    groups.sort(key=lambda g: g.anchor.uri)
    return groups


def _group(anchor: FileItem, members: list[FileItem]) -> Group:
    return Group(anchor=anchor, members=[Member(m, 1.0) for m in members], method="identical")
