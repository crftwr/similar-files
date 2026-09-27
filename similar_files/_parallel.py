"""Thread pools for extraction, with cancellation and progress on the caller's thread."""

from __future__ import annotations

import concurrent.futures as cf
import os
import threading
from typing import Callable, Optional, TypeVar

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
        self.report()

    def _count(self, _future) -> None:
        with self._lock:
            self._done += 1

    def report(self) -> None:
        if self._progress is None:
            return
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
