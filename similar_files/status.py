"""A scan's status as a small JSON file, for a program watching from outside.

A long scan (similar videos can take hours) is often started by another
program that does not stay around for it — XeFM launches ``similar-files
scan`` as its own process so that closing the file manager does not stop the
scan. That program learns how the scan is going from the file
``--status`` names, and can stop it by creating the file ``--stop-file``
names; neither needs the two to share a process, a pipe or a signal, so it
works the same on every platform and across a restart of the watcher.

The file is replaced whole on every write (a temporary file, then
:func:`os.replace`), so a reader never sees half of it. While the scan runs
it is rewritten at least every :data:`HEARTBEAT` seconds even when nothing
moves — an extractor can spend minutes on one video — so a reader can tell a
scan that is busy from one that died: ``updated`` stops changing.

Fields (``version`` 1):

``state``
    ``"running"``, then one of ``"done"``, ``"cancelled"`` or ``"failed"``.
``pid``
    The scanning process.
``started``, ``updated``
    Seconds since the epoch.
``phase``, ``done``, ``total``
    The latest progress report (see :class:`~similar_files.model.Progress`);
    ``total`` is 0 when not known.
``groups``, ``members``
    Written when the scan ends: what the playlists hold.
``out``
    The playlist folder.
``exit_code``
    Written when the scan ends: what the command returns.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .model import Progress

VERSION = 1

#: Most seconds between two writes while a scan runs.
HEARTBEAT = 5.0
#: Fewest seconds between two writes caused by progress.
MIN_INTERVAL = 1.0

RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"
FINISHED = frozenset({DONE, CANCELLED, FAILED})


def read_status(path: str | os.PathLike) -> Optional[dict]:
    """The status in ``path``, or None when there is none yet or it is not
    one this version understands."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None
    return data


class StatusFile:
    """Writes the status file for one scan. ``update`` is the scan's
    progress callback (any thread); ``finish`` writes the final state."""

    def __init__(self, path: str | os.PathLike, *, out: str | os.PathLike,
                 heartbeat: float = HEARTBEAT, min_interval: float = MIN_INTERVAL):
        self.path = Path(path)
        self.heartbeat = heartbeat
        self.min_interval = min_interval
        now = time.time()
        self._data: dict[str, Any] = {
            "version": VERSION, "state": RUNNING, "pid": os.getpid(),
            "started": now, "updated": now, "phase": None, "done": 0, "total": 0,
            "out": str(out),
        }
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "StatusFile":
        self._write()
        self._thread = threading.Thread(target=self._beat, name="similar-files-status", daemon=True)
        self._thread.start()
        return self

    def update(self, p: Progress) -> None:
        with self._lock:
            phase_changed = p.phase != self._data["phase"]
            self._data.update(phase=p.phase, done=p.done, total=p.total)
            due = phase_changed or time.monotonic() - self._last_write >= self.min_interval
        if due:
            self._write()

    def finish(self, state: str, exit_code: int, *, groups: int = 0, members: int = 0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat + 1)
        with self._lock:
            self._data.update(state=state, exit_code=exit_code, groups=groups, members=members)
        self._write()

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat):
            self._write()

    def _write(self) -> None:
        with self._lock:
            self._data["updated"] = time.time()
            text = json.dumps(self._data, ensure_ascii=False)
            self._last_write = time.monotonic()
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                # A status file that cannot be written must not stop the scan;
                # the watcher sees ``updated`` go stale and says so.
                pass


class StopFile:
    """``cancel`` callable that turns true once ``path`` exists. Looked at no
    more often than every ``interval`` seconds, since a scan asks often."""

    def __init__(self, path: str | os.PathLike, interval: float = 0.5):
        self.path = Path(path)
        self.interval = interval
        self._next = 0.0
        self._seen = False

    def __call__(self) -> bool:
        if self._seen:
            return True
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.interval
            self._seen = self.path.exists()
        return self._seen
