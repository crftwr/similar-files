"""Layers 2 and 3: extract features through the registry, then group by similarity.

Extraction and grouping are separate steps. :func:`extract_features` does
the slow part once (and the cache makes it once per file ever);
:func:`group_features` can then run again with another threshold without
re-extracting.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from ._parallel import Runner
from .cache import Cache, NullCache
from .identical import _CHUNK, _CACHE_BATCH, _CACHE_SECONDS, _open_cache, _same_identity
from .model import Cancelled, CancelCheck, Group, Member, Progress, ProgressCallback, ScanResult
from .registry import ExtractionFailed, Extractor, _cancel_scope, get_extractor
from .source import FileItem

logger = logging.getLogger(__name__)


@dataclass
class FeatureSet:
    """Features extracted for one extractor, ready to group (and regroup)."""

    extractor: Extractor
    items: list[FileItem] = field(default_factory=list)
    features: list[bytes] = field(default_factory=list)
    references: list[FileItem] = field(default_factory=list)
    reference_features: list[bytes] = field(default_factory=list)
    #: Errors, skipped remote files and cancellation, carried into the scan result.
    result: ScanResult = field(default_factory=ScanResult)


def extract_features(
    items: Iterable[FileItem],
    extractor: "Extractor | str" = "image",
    *,
    references: Sequence[FileItem] = (),
    params: Optional[dict[str, Any]] = None,
    cache: "Cache | NullCache | str | os.PathLike | None | bool" = None,
    include_remote: bool = False,
    workers: Optional[int] = None,
    remote_workers: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[CancelCheck] = None,
) -> FeatureSet:
    """Extract (or fetch from the cache) the features of every item the extractor handles.

    Items the extractor does not handle are ignored. Remote items
    (``is_remote``) are skipped and counted unless ``include_remote`` is
    true, because extracting means downloading the whole file. References
    are always extracted: the caller named them. Each file is read once: its
    content hash is computed in the same pass as its feature.
    """
    if isinstance(extractor, str):
        extractor = get_extractor(extractor, **(params or {}))
    elif params:
        raise ValueError("pass params only with an extractor name")
    owned, cache_obj = _open_cache(cache)
    try:
        return _extract(items, extractor, references, cache_obj, include_remote, workers, remote_workers, progress, cancel)
    finally:
        if owned:
            cache_obj.close()


def _extract(items, extractor, references, cache, include_remote, workers, remote_workers, progress, cancel):
    fs = FeatureSet(extractor=extractor)
    result = fs.result
    key = extractor.feature_key
    refs = [r for r in references if extractor.handles(r)]
    for r in references:
        if not extractor.handles(r):
            logger.warning("%s: not a file the %r extractor reads; ignored as a reference", r.uri, extractor.name)

    # (item, is_ref); duplicates by URI and references among the items are dropped.
    todo: list[tuple[FileItem, bool]] = [(r, True) for r in refs]
    seen = {r.uri for r in refs}
    for item in items:
        if cancel is not None and cancel():
            result.cancelled = True
            return fs
        if item.uri in seen or not extractor.handles(item):
            continue
        if any(_same_identity(item, r) for r in refs):
            continue
        if item.is_remote and not include_remote:
            result.skipped_remote += 1
            continue
        seen.add(item.uri)
        todo.append((item, False))
    if result.skipped_remote:
        logger.warning("skipped %d remote file(s); extracting them means downloading them", result.skipped_remote)

    found: dict[int, bytes] = {}  # index in todo -> feature
    touched: list[tuple[str, Any]] = []
    misses: list[int] = []

    # Features are keyed by the file (URI, size, mtime, validator), not by
    # its content: a cache hit needs no reading at all, and a miss is read
    # once, by the extractor.
    for i, (item, _) in enumerate(todo):
        hit, data = cache.feature(item.uri, item.size, item.mtime_ns, item.validator, key)
        if not hit:
            misses.append(i)
            continue
        if progress is not None:
            progress(Progress("cache", i + 1, len(todo), item.uri, "cached"))
        touched.append((item.uri, key))
        if data is None:
            result.unreadable += 1
        else:
            found[i] = data

    def download(item: FileItem, stop: threading.Event, out) -> None:
        with item.open() as f:
            while True:
                if stop.is_set():
                    raise Cancelled()
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)

    def work(item: FileItem, stop: threading.Event, note) -> Optional[bytes]:
        """The feature, or ``None`` if the extractor cannot read the file."""
        if stop.is_set():
            raise Cancelled()
        note("start", item.uri)
        started = time.monotonic()
        try:
            # run_program() in the extractor sees ``stop``, and kills its program on cancel.
            with _cancel_scope(stop):
                data = extract(item, stop)
            note("done", item.uri, time.monotonic() - started)
            return data
        except ExtractionFailed as exc:
            if exc.transient:
                note("failed", item.uri)
                raise
            logger.warning("%s: %s cannot read it: %s", item.uri, extractor.name, exc)
            note("unreadable", item.uri, time.monotonic() - started)
            return None
        except Cancelled:
            raise
        except Exception:
            note("failed", item.uri)
            raise

    def extract(item: FileItem, stop: threading.Event) -> bytes:
        local = getattr(item, "local_path", None)
        if local:
            if extractor.needs_path:
                return extractor.extract_file(local)
            with open(local, "rb") as f:
                return extractor.extract(f)
        if not extractor.needs_path:
            buf = io.BytesIO()
            download(item, stop, buf)
            buf.seek(0)
            return extractor.extract(buf)
        # The tool that extracts (ffmpeg, fpcalc) opens a file by name, and a
        # container such as MP4 needs to seek, so it cannot read a pipe.
        # Remote: download once into a temporary file.
        suffix = os.path.splitext(item.uri.rsplit("/", 1)[-1])[1][:16]
        fd, tmp = tempfile.mkstemp(prefix="similar-files-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as out:
                download(item, stop, out)
            return extractor.extract_file(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    new_features: list[tuple] = []
    last_flush = time.monotonic()

    def flush(force: bool = False) -> None:
        nonlocal last_flush
        if new_features and (force or len(new_features) >= _CACHE_BATCH or time.monotonic() - last_flush >= _CACHE_SECONDS):
            cache.put_features(new_features)
            new_features.clear()
            last_flush = time.monotonic()

    with Runner(workers=workers, remote_workers=remote_workers, progress=progress, cancel=cancel) as runner:
        runner.start_phase("extract", len(misses))
        futures = {runner.submit(todo[i][0].is_remote, work, todo[i][0], runner.stop, runner.note): i for i in misses}
        # In the order they finish: a long file does not hold back the ones
        # behind it, and a cancelled scan still keeps every finished file.
        for future in runner.as_completed(futures):
            i = futures[future]
            item = todo[i][0]
            try:
                data = future.result()
            except Cancelled:
                continue
            except Exception as exc:  # noqa: BLE001 - one unreadable file never aborts a scan
                logger.warning("cannot read %s: %s", item.uri, exc)
                result.add_error(item.uri)
                continue
            new_features.append((item.uri, item.size, item.mtime_ns, item.validator, key, data))
            if data is None:
                result.unreadable += 1
            else:
                found[i] = data
            flush()
        result.cancelled = runner.stop.is_set()
        # Everything finished so far is valid, cancelled or not.
        flush(force=True)
        cache.touch(features=touched)

    for i, (item, is_ref) in enumerate(todo):
        data = found.get(i)
        if data is None:
            continue
        if is_ref:
            fs.references.append(item)
            fs.reference_features.append(data)
        else:
            fs.items.append(item)
            fs.features.append(data)
    return fs


def group_features(
    fs: FeatureSet,
    threshold: Optional[float] = None,
    *,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[CancelCheck] = None,
) -> list[Group]:
    """Group extracted features, star-shaped, at ``threshold`` (0–1; the extractor's default if ``None``).

    With references, each reference anchors a group of the items within the
    threshold of it. Without, anchors are picked from the items, largest file
    first; an item already in a group is never picked as an anchor, but may
    still be a member of a later group. Returns an empty list if cancelled.
    """
    ex = fs.extractor
    if threshold is None:
        threshold = ex.default_threshold
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    prepared = ex.prepare(fs.features)

    def make(anchor: FileItem, sims: Sequence[float], exclude: Optional[int]) -> tuple[Optional[Group], list[int]]:
        near = [j for j, s in enumerate(sims) if j != exclude and s >= threshold]
        if not near:
            return None, near
        members = sorted((Member(fs.items[j], float(sims[j])) for j in near), key=lambda m: (-m.similarity, m.item.uri))
        return Group(anchor, members, ex.name, ex.version, dict(ex.params), threshold), near

    groups: list[Group] = []
    if fs.references:
        total = len(fs.references)
        for n, (ref, feature) in enumerate(zip(fs.references, fs.reference_features)):
            if cancel is not None and cancel():
                return []
            if progress is not None:
                progress(Progress("group", n, total))
            group, _ = make(ref, ex.similarities(prepared, feature), None)
            if group is not None:
                groups.append(group)
        if progress is not None:
            progress(Progress("group", total, total))
        return groups

    order = sorted(range(len(fs.items)), key=lambda i: (-fs.items[i].size, fs.items[i].uri))
    grouped = [False] * len(fs.items)
    total = len(order)
    for n, i in enumerate(order):
        if cancel is not None and cancel():
            return []
        if progress is not None and n % 64 == 0:
            progress(Progress("group", n, total))
        if grouped[i]:
            continue
        group, near = make(fs.items[i], ex.similarities(prepared, fs.features[i]), i)
        if group is None:
            continue
        grouped[i] = True
        for j in near:
            grouped[j] = True
        groups.append(group)
    if progress is not None:
        progress(Progress("group", total, total))
    return groups


def find_similar(
    items: Iterable[FileItem],
    extractor: "Extractor | str" = "image",
    *,
    threshold: Optional[float] = None,
    references: Sequence[FileItem] = (),
    params: Optional[dict[str, Any]] = None,
    cache: "Cache | NullCache | str | os.PathLike | None | bool" = None,
    include_remote: bool = False,
    workers: Optional[int] = None,
    remote_workers: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[CancelCheck] = None,
) -> ScanResult:
    """Extract features and group them in one call. See :func:`extract_features` and :func:`group_features`."""
    fs = extract_features(
        items,
        extractor,
        references=references,
        params=params,
        cache=cache,
        include_remote=include_remote,
        workers=workers,
        remote_workers=remote_workers,
        progress=progress,
        cancel=cancel,
    )
    result = fs.result
    if not result.cancelled:
        result.groups = group_features(fs, threshold, progress=progress, cancel=cancel)
        if cancel is not None and cancel():
            result.cancelled = True
    return result
