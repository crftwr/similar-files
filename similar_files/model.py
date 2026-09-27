"""Result types shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .source import FileItem


@dataclass
class Member:
    """One file in a group, with its similarity to the group's anchor (0–1, 1.0 is identical)."""

    item: FileItem
    similarity: float


@dataclass
class Group:
    """An anchor and the files within the threshold of it.

    Grouping is star-shaped: each member is close to the anchor, not
    necessarily to the other members. Members are sorted nearest first.
    """

    anchor: FileItem
    members: list[Member]
    #: ``"identical"``, or the name of the extractor that compared the files.
    method: str
    #: The extractor's version and parameters (empty for identical files).
    version: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    #: The similarity threshold the group was made with.
    threshold: float = 1.0

    @property
    def items(self) -> list[FileItem]:
        """The anchor followed by the members."""
        return [self.anchor] + [m.item for m in self.members]


@dataclass
class Progress:
    """A progress report, delivered on the thread that runs the scan.

    ``phase`` is one of ``"sample"`` (hashing a head-and-tail sample),
    ``"hash"`` (hashing whole files), ``"extract"`` (reading features) and
    ``"group"``. ``total`` may be 0 when it is not known.
    """

    phase: str
    done: int
    total: int


ProgressCallback = Callable[[Progress], None]
CancelCheck = Callable[[], bool]


@dataclass
class ScanResult:
    """What a scan found, and what it could not read.

    ``cancelled`` is true when the scan stopped early; ``groups`` then holds
    the groups completed before it stopped, and every cache row written is
    valid.
    """

    groups: list[Group] = field(default_factory=list)
    cancelled: bool = False
    #: Files that could not be read, or whose features could not be extracted.
    errors: int = 0
    error_uris: list[str] = field(default_factory=list)
    #: Remote files the perceptual layer skipped (``include_remote`` was false).
    skipped_remote: int = 0
    #: Files the extractor recognized but could not decode (cached, not retried).
    unreadable: int = 0

    def add_error(self, uri: str) -> None:
        self.errors += 1
        self.error_uris.append(uri)


class Cancelled(Exception):
    """Raised inside a worker to abandon its file when the scan is cancelled."""
