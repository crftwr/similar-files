"""Thread pools for extraction, with cancellation and progress on the caller's thread."""

from __future__ import annotations

import collections
import concurrent.futures as cf
import os
import threading
from typing import Callable, Iterator, Optional, TypeVar

from .model import CancelCheck, Progress, ProgressCallback

T = TypeVar("T")

#: Workers for local files: capped by the CPU count.
DEFAULT_MAX_WORKERS = 8
#: Workers for remote files: a server is not a CPU.
DEFAULT_REMOTE_WORKERS = 4

_POLL_SECONDS = 0.1


def default_workers() -> int:
    return max(1, min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1))


class Runner:
    """Two thread pools (local and remote items), a cancel flag, and progress counting.

    Jobs run on the pools; waiting, progress callbacks and the cancel check
    run on the thread that owns the runner.
    """

    def __init__(
        self,
        *,
        workers: Optional[int] = None,
        remote_workers: Optional[int] = None,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[CancelCheck] = None,
    ):
        self._local = cf.ThreadPoolExecutor(max_workers=workers or default_workers(), thread_name_prefix="sf-local")
        self._remote_workers = remote_workers or DEFAULT_REMOTE_WORKERS
        self._remote: Optional[cf.ThreadPoolExecutor] = None
        self._progress = progress
        self._cancel = cancel
        #: Set when the scan is cancelled; workers check it between chunks.
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._phase = ""
        self._done = 0
        self._total = 0
        self._reported = (None, -1, -1)
        #: Per-file events from workers, delivered by :meth:`report` on the owner's thread.
        self._events: collections.deque = collections.deque()
        #: Files reported finished in this phase, numbering the per-file events
        #: in the order they are delivered.
        self._finished = 0

    def submit(self, remote: bool, fn: Callable[..., T], *args) -> "cf.Future[T]":
        if remote:
            if self._remote is None:
                self._remote = cf.ThreadPoolExecutor(max_workers=self._remote_workers, thread_name_prefix="sf-remote")
            pool = self._remote
        else:
            pool = self._local
        future = pool.submit(fn, *args)
        future.add_done_callback(self._count)
        return future

    def start_phase(self, phase: str, total: int) -> None:
        with self._lock:
            self._phase, self._done, self._total = phase, 0, total
            self._finished = 0
        self.report()

    def _count(self, _future) -> None:
        with self._lock:
            self._done += 1

    def note(self, event: str, uri: str, seconds: Optional[float] = None) -> None:
        """Record a per-file event (see :class:`Progress`). Any thread may call it."""
        if self._progress is not None:
            self._events.append((event, uri, seconds))

    def report(self) -> None:
        if self._progress is None:
            return
        while self._events:
            event, uri, seconds = self._events.popleft()
            with self._lock:
                if event != "start":
                    self._finished += 1
                phase, done, total = self._phase, self._finished, self._total
            self._progress(Progress(phase, done, total, uri, event, seconds))
        with self._lock:
            state = (self._phase, self._done, self._total)
        if state != self._reported:
            self._reported = state
            self._progress(Progress(*state))

    def cancelled(self) -> bool:
        """Poll the caller's cancel check; once true, stays true."""
        if not self.stop.is_set() and self._cancel is not None and self._cancel():
            self.stop.set()
        return self.stop.is_set()

    def wait(self, futures) -> bool:
        """Wait for ``futures``, reporting progress. False if the scan was cancelled first."""
        pending = set(futures)
        while pending:
            if self.cancelled():
                return False
            _, pending = cf.wait(pending, timeout=_POLL_SECONDS)
            self.report()
        return not self.cancelled()

    def as_completed(self, futures) -> Iterator["cf.Future"]:
        """Yield ``futures`` as they finish, reporting progress.

        If the scan is cancelled, yields those already finished by then, and
        stops; :attr:`stop` is then set. Nothing that finished is lost, so
        the caller can keep every completed result.
        """
        pending = set(futures)
        while pending:
            if self.cancelled():
                self.report()
                yield from [f for f in pending if f.done()]
                return
            done, pending = cf.wait(pending, timeout=_POLL_SECONDS, return_when=cf.FIRST_COMPLETED)
            self.report()
            yield from done

    def shutdown(self) -> None:
        """Stop handing out work. Running jobs see :attr:`stop` if cancelled, and are not waited for."""
        cancelled = self.stop.is_set()
        for pool in (self._local, self._remote):
            if pool is not None:
                pool.shutdown(wait=not cancelled, cancel_futures=True)

    def __enter__(self) -> "Runner":
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is not None:
            self.stop.set()
        self.shutdown()
