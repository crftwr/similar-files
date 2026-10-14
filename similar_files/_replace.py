"""Replacing a file another process may be reading.

The status file and the ``.xft`` tree are both written whole to a ``.tmp``
beside them and moved over the old one, so a reader never sees half of either.
On Windows that move fails with a sharing violation (``PermissionError``) while
any process has the target open, and the program watching a scan reads both
files as it goes. The window is a read's length, so the move is tried again
for a short while rather than given up on: given up, the status file kept
saying "running" after the scan had finished, and the watcher took it for lost.
"""

from __future__ import annotations

import os
import time

#: How long to keep trying, in all, and how long to wait between tries.
RETRY_FOR = 2.0
RETRY_EVERY = 0.02


def replace(src: str | os.PathLike, dst: str | os.PathLike, *,
            retry_for: float = RETRY_FOR, every: float = RETRY_EVERY) -> None:
    """:func:`os.replace`, tried again while it fails with ``PermissionError``
    for up to ``retry_for`` seconds. The last failure is raised."""
    deadline = time.monotonic() + retry_for
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(every)
