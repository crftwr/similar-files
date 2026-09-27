"""The extractor registry: media kind → feature extractor → similarity.

Perceptual and semantic layers are entries here, not code paths. A third
party registers its own with :func:`register`.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, ClassVar, Iterator, Optional, Sequence

from .cache import FeatureKey
from .model import Cancelled
from .source import FileItem

logger = logging.getLogger(__name__)

#: How often a running program is checked for the scan being cancelled.
_POLL_SECONDS = 0.1
#: The cancel flag of the scan the current worker thread is extracting for.
_scope = threading.local()


class ExtractionFailed(Exception):
    """The extractor recognizes the file type but cannot read this file (corrupt, truncated…).

    The failure is cached, so the file is not tried again until it changes
    or the extractor's version does. A ``transient`` failure (a timeout, a
    program that could not be started) says nothing about the file: it is
    counted as an error and not cached, so the next scan tries again.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class ExtractorUnavailable(Exception):
    """The extractor's optional dependency is not installed."""

    def __init__(self, name: str, install_hint: str):
        super().__init__(f"extractor {name!r} is unavailable: {install_hint}")
        self.name = name
        self.install_hint = install_hint


class Extractor:
    """Base class for a feature extractor.

    A subclass sets the class attributes and implements :meth:`extract` and
    :meth:`similarity`. The extractor owns its feature's encoding (opaque
    bytes to the cache) and its distance; it maps that distance to a
    similarity where 1.0 is identical and 0.0 is unrelated.
    """

    #: Stable name. Part of the cache key.
    name: ClassVar[str]
    #: Bump whenever the extractor's output changes for the same input.
    version: ClassVar[int]
    #: Lower-case file extensions this extractor handles, with the dot.
    extensions: ClassVar[frozenset[str]] = frozenset()
    #: Importable module names the extractor needs.
    requires: ClassVar[tuple[str, ...]] = ()
    #: The ``pip install similar-files[extra]`` that provides ``requires``.
    extra: ClassVar[Optional[str]] = None
    #: Parameters that change the output, with their defaults. Part of the cache key.
    default_params: ClassVar[dict[str, Any]] = {}
    #: The similarity threshold used when the caller gives none.
    default_threshold: ClassVar[float] = 0.9
    #: One line for listings.
    description: ClassVar[str] = ""
    #: Extract with :meth:`extract_file` from a path on disk rather than
    #: :meth:`extract` from an in-memory stream: for files too big to hold in
    #: memory, or read by an external program (ffmpeg, fpcalc).
    needs_path: ClassVar[bool] = False
    #: External programs the extractor runs, looked up on ``PATH``.
    requires_programs: ClassVar[tuple[str, ...]] = ()

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"extractor {self.name!r} has no parameter(s) {', '.join(sorted(unknown))}")
        merged = dict(self.default_params)
        for key, value in params.items():
            default = self.default_params[key]
            if default is not None and not isinstance(value, type(default)):
                value = coerce_param(value, type(default))
            merged[key] = value
        self.validate_params(merged)
        self.params: dict[str, Any] = merged

    def validate_params(self, params: dict[str, Any]) -> None:
        """Raise ``ValueError`` for parameter values the extractor cannot use."""

    @classmethod
    def is_available(cls) -> bool:
        return all(importlib.util.find_spec(m) is not None for m in cls.requires) and all(
            shutil.which(p) is not None for p in cls.requires_programs
        )

    @classmethod
    def install_hint(cls) -> str:
        hints = []
        if any(importlib.util.find_spec(m) is None for m in cls.requires):
            hints.append(f'pip install "similar-files[{cls.extra}]"' if cls.extra else "install " + ", ".join(cls.requires))
        missing = [p for p in cls.requires_programs if shutil.which(p) is None]
        if missing:
            hints.append("put " + ", ".join(missing) + " on PATH")
        return "; ".join(hints) or "available"

    @property
    def params_digest(self) -> str:
        """A canonical digest of the parameters, for the cache key."""
        canonical = json.dumps(self.params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @property
    def feature_key(self) -> FeatureKey:
        return FeatureKey(self.name, self.version, self.params_digest)

    def handles(self, item: FileItem) -> bool:
        """Whether this extractor reads ``item``. The default goes by extension."""
        return os.path.splitext(item.uri.rsplit("/", 1)[-1])[1].lower() in self.extensions

    def extract(self, stream: BinaryIO) -> bytes:
        """The feature of the file whose content ``stream`` holds (seekable).

        Raise :class:`ExtractionFailed` for content this extractor cannot
        read. Called on a worker thread.
        """
        raise NotImplementedError

    def extract_file(self, path: str) -> bytes:
        """The feature of the local file at ``path``. Used instead of
        :meth:`extract` when :attr:`needs_path` is true.

        ``path`` is the item's own ``local_path`` when it has one, or a
        temporary copy of a remote item, deleted afterwards. Never write to
        it. Raise :class:`ExtractionFailed` as for :meth:`extract`. Run
        external programs with :func:`run_program`, so a cancelled scan
        stops them.
        """
        with open(path, "rb") as f:
            return self.extract(f)

    def similarity(self, a: bytes, b: bytes) -> float:
        """Similarity of two features, 0–1."""
        raise NotImplementedError

    def prepare(self, features: Sequence[bytes]) -> Any:
        """Turn features into whatever makes :meth:`similarities` fast. The default keeps them."""
        return list(features)

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        """Similarity of ``query`` to each prepared feature, in order."""
        return [self.similarity(query, f) for f in prepared]


def coerce_param(value: Any, kind: type) -> Any:
    """Convert a parameter given as text (from the command line) to its type."""
    if kind is bool and isinstance(value, str):
        low = value.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    return kind(value)


_REGISTRY: dict[str, type[Extractor]] = {}
_builtins_loaded = False


def register(cls: type[Extractor]) -> type[Extractor]:
    """Register an extractor class. Usable as a decorator. A later one replaces an earlier one of the same name."""
    if not getattr(cls, "name", None) or not isinstance(getattr(cls, "version", None), int):
        raise TypeError(f"{cls.__name__} needs a name and an integer version")
    _REGISTRY[cls.name] = cls
    return cls


def _load_builtins() -> None:
    global _builtins_loaded
    if not _builtins_loaded:
        _builtins_loaded = True
        from . import audio, image, video  # noqa: F401 - each registers itself


def extractors() -> dict[str, type[Extractor]]:
    """Every registered extractor class, available or not, by name."""
    _load_builtins()
    return dict(_REGISTRY)


def get_extractor(name: str, **params: Any) -> Extractor:
    """An instance of the extractor ``name`` with ``params``.

    Raises ``KeyError`` for an unknown name and :class:`ExtractorUnavailable`
    when its dependency is missing.
    """
    _load_builtins()
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no extractor named {name!r}; known: {', '.join(sorted(_REGISTRY))}") from None
    if not cls.is_available():
        raise ExtractorUnavailable(name, cls.install_hint())
    return cls(**params)


@contextlib.contextmanager
def _cancel_scope(stop: threading.Event) -> Iterator[None]:
    """Let :func:`run_program` on this thread see ``stop``, the scan's cancel flag."""
    previous = getattr(_scope, "stop", None)
    _scope.stop = stop
    try:
        yield
    finally:
        _scope.stop = previous


def run_program(args: Sequence[str], *, timeout: float) -> "subprocess.CompletedProcess[bytes]":
    """Run an external program for an extractor, capturing its output.

    - The program runs in a process group of its own, so a Ctrl-C in the
      terminal reaches the scan (which cancels cleanly) and not the program
      (which would fail and be cached as an unreadable file).
    - If the scan is cancelled while it runs, the program is killed and
      :class:`~similar_files.model.Cancelled` is raised: nothing is cached.
    - Running longer than ``timeout`` seconds, or failing to start, raises a
      transient :class:`ExtractionFailed`, which is not cached either.

    A non-zero exit status is returned, not raised: what it means is the
    extractor's to decide.
    """
    stop: Optional[threading.Event] = getattr(_scope, "stop", None)
    if stop is not None and stop.is_set():
        raise Cancelled()
    if sys.platform == "win32":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group
        )
    except OSError as exc:
        raise ExtractionFailed(f"cannot run {args[0]}: {exc}", transient=True) from exc
    deadline = time.monotonic() + timeout
    with proc:
        while True:
            try:
                out, err = proc.communicate(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if stop is not None and stop.is_set():
                    proc.kill()
                    proc.communicate()
                    raise Cancelled() from None
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.communicate()
                    raise ExtractionFailed(f"{args[0]} took longer than {timeout:g}s", transient=True) from None
    return subprocess.CompletedProcess(list(args), proc.returncode, out, err)
