"""The feature cache: one SQLite database per user, shared by every caller.

It is a cache, not state. Deleting it loses nothing but time. The schema is
a contract other programs may read; it is documented in ``doc/CACHE.md``.

Threading: a :class:`Cache` has one *writer*, the thread that runs a scan.
Worker threads read through connections of their own (:meth:`Cache.reader`),
which WAL mode allows while the writer writes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DB_FILENAME = "cache.sqlite3"
ENV_VAR = "SIMILAR_FILES_CACHE"
_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    uri          BLOB PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    validator    TEXT,
    content_hash TEXT NOT NULL,
    last_seen    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS features (
    content_hash      TEXT NOT NULL,
    extractor         TEXT NOT NULL,
    extractor_version INTEGER NOT NULL,
    params_digest     TEXT NOT NULL,
    data              BLOB,
    last_seen         INTEGER NOT NULL,
    PRIMARY KEY (content_hash, extractor, extractor_version, params_digest)
);
CREATE INDEX IF NOT EXISTS files_last_seen ON files (last_seen);
CREATE INDEX IF NOT EXISTS features_last_seen ON features (last_seen);
"""


class CacheError(Exception):
    """The cache cannot be opened."""


def default_cache_dir() -> Path:
    """The platform's per-user cache directory for similar-files.

    This is the one place the rule is written. The environment variable is
    not consulted here; see :func:`default_cache_path`.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "similar-files"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "similar-files" / "Cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "similar-files"


def resolve_cache_path(path: Optional[str | os.PathLike] = None) -> Path:
    """The database file to use.

    ``path`` (from ``--cache`` or a caller's setting) wins, then the
    ``SIMILAR_FILES_CACHE`` environment variable, then the platform default.
    A value naming an existing directory, or ending in a path separator,
    means the database file inside it.
    """
    raw = os.fspath(path) if path is not None else os.environ.get(ENV_VAR)
    if not raw:
        return default_cache_dir() / DB_FILENAME
    p = Path(raw).expanduser()
    if raw.endswith(("/", os.sep)) or p.is_dir():
        return p / DB_FILENAME
    return p


def _uri_key(uri: str) -> bytes:
    # Filenames are bytes on POSIX; os.fsdecode() turns undecodable bytes into
    # surrogates, which SQLite's TEXT cannot hold. Store the exact bytes.
    return uri.encode("utf-8", "surrogateescape")


@dataclass(frozen=True)
class FeatureKey:
    """Identifies one kind of feature: which extractor, which version, which parameters."""

    extractor: str
    version: int
    params_digest: str


@dataclass
class GcResult:
    files_removed: int
    features_removed: int


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def _read_feature(conn: sqlite3.Connection, content_hash: str, key: FeatureKey) -> tuple[bool, Optional[bytes]]:
    row = conn.execute(
        "SELECT data FROM features WHERE content_hash=? AND extractor=? AND extractor_version=? AND params_digest=?",
        (content_hash, key.extractor, key.version, key.params_digest),
    ).fetchone()
    if row is None:
        return False, None
    return True, row[0]


class CacheReader:
    """A read-only view for a worker thread. Create one per thread."""

    def __init__(self, path: Path):
        self._conn = _connect(path)

    def feature(self, content_hash: str, key: FeatureKey) -> tuple[bool, Optional[bytes]]:
        """``(found, data)``. ``data`` is ``None`` when the extractor could not read the file."""
        return _read_feature(self._conn, content_hash, key)

    def close(self) -> None:
        self._conn.close()


class Cache:
    """The feature cache.

    Use as a context manager, or call :meth:`close`. All write methods must be
    called from one thread at a time; the scan functions call them only from
    the thread they run on.
    """

    def __init__(self, path: Optional[str | os.PathLike] = None):
        self.path = resolve_cache_path(path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = _connect(self.path)
            self._retry_while_locked(self._setup)
        except (sqlite3.Error, OSError, CacheError) as exc:
            if self._conn is not None:
                self._conn.close()
            if isinstance(exc, CacheError):
                raise
            raise CacheError(f"cannot open cache {self.path}: {exc}") from exc

    def _retry_while_locked(self, fn) -> None:
        # Two processes opening a new cache at once: changing the journal mode
        # does not wait on the busy handler, so wait here.
        deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
        delay = 0.01
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc) or time.monotonic() > deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    def _setup(self) -> None:
        # Check before changing anything: journal_mode is stored in the file.
        self._check_ours()
        if self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def _check_ours(self) -> None:
        # One statement, one snapshot: another opener may commit the schema in between.
        version, tables = self._conn.execute(
            "SELECT (SELECT user_version FROM pragma_user_version), (SELECT COUNT(*) FROM sqlite_master)"
        ).fetchone()
        if version == 0 and tables:
            raise CacheError(f"{self.path} is a database, but not a similar-files cache")
        if version > SCHEMA_VERSION:
            raise CacheError(
                f"cache {self.path} has schema version {version}, newer than this "
                f"similar-files understands ({SCHEMA_VERSION}); use another --cache"
            )

    def _init_schema(self) -> None:
        if self._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have just done this.
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                if version != 0:
                    # Older schema. It is only a cache: start over.
                    logger.info("cache %s: schema %d replaced by %d", self.path, version, SCHEMA_VERSION)
                    self._conn.execute("DROP TABLE IF EXISTS files")
                    self._conn.execute("DROP TABLE IF EXISTS features")
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        self._conn.execute(stmt)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # -- reading -----------------------------------------------------------

    def content_hash(self, uri: str, size: int, mtime_ns: int, validator: Optional[str] = None) -> Optional[str]:
        """The cached content hash for a file, if its stat still matches."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime_ns, validator, content_hash FROM files WHERE uri=?", (_uri_key(uri),)
            ).fetchone()
        if row is None:
            return None
        if row[0] != size or row[1] != mtime_ns or row[2] != validator:
            return None
        return row[3]

    def feature(self, content_hash: str, key: FeatureKey) -> tuple[bool, Optional[bytes]]:
        """``(found, data)``. ``data`` is ``None`` when the extractor could not read the file."""
        with self._lock:
            return _read_feature(self._conn, content_hash, key)

    def reader(self) -> "CacheReader":
        """A new read connection for a worker thread. The caller closes it."""
        return CacheReader(self.path)

    # -- writing (one thread) ---------------------------------------------

    def put_hashes(self, rows: Iterable[tuple[str, int, int, Optional[str], str]]) -> None:
        """Store ``(uri, size, mtime_ns, validator, content_hash)`` rows in one transaction."""
        now = int(time.time())
        data = [(_uri_key(u), s, m, v, h, now) for u, s, m, v, h in rows]
        if data:
            self._write(
                "INSERT OR REPLACE INTO files (uri, size, mtime_ns, validator, content_hash, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                data,
            )

    def put_features(self, rows: Iterable[tuple[str, FeatureKey, Optional[bytes]]]) -> None:
        """Store ``(content_hash, key, data)`` rows in one transaction. ``data=None`` records a failure."""
        now = int(time.time())
        data = [(h, k.extractor, k.version, k.params_digest, d, now) for h, k, d in rows]
        if data:
            self._write(
                "INSERT OR REPLACE INTO features "
                "(content_hash, extractor, extractor_version, params_digest, data, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                data,
            )

    def touch(self, uris: Iterable[str] = (), features: Iterable[tuple[str, FeatureKey]] = ()) -> None:
        """Mark rows as seen now, so garbage collection keeps them."""
        now = int(time.time())
        file_rows = [(now, _uri_key(u)) for u in uris]
        feature_rows = [(now, h, k.extractor, k.version, k.params_digest) for h, k in features]
        with self._lock:
            self._begin()
            try:
                if file_rows:
                    self._conn.executemany("UPDATE files SET last_seen=? WHERE uri=?", file_rows)
                if feature_rows:
                    self._conn.executemany(
                        "UPDATE features SET last_seen=? WHERE content_hash=? AND extractor=? "
                        "AND extractor_version=? AND params_digest=?",
                        feature_rows,
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _write(self, sql: str, rows: list) -> None:
        with self._lock:
            self._begin()
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # -- maintenance -------------------------------------------------------

    def gc(self, *, max_age_days: Optional[float] = None, max_bytes: Optional[int] = None) -> GcResult:
        """Remove rows not seen for ``max_age_days``, then the oldest features over ``max_bytes``.

        Never removes a row because its file is missing right now: an
        unplugged drive or an unreachable server is not a deleted file.
        """
        files_removed = features_removed = 0
        with self._lock:
            self._begin()
            try:
                if max_age_days is not None:
                    cutoff = int(time.time() - max_age_days * 86400)
                    files_removed += self._conn.execute("DELETE FROM files WHERE last_seen < ?", (cutoff,)).rowcount
                    features_removed += self._conn.execute(
                        "DELETE FROM features WHERE last_seen < ?", (cutoff,)
                    ).rowcount
                if max_bytes is not None:
                    total = self._conn.execute(
                        "SELECT COALESCE(SUM(LENGTH(data)), 0) + COUNT(*) * 128 FROM features"
                    ).fetchone()[0]
                    if total > max_bytes:
                        excess = total - max_bytes
                        doomed = []
                        for rowid, size in self._conn.execute(
                            "SELECT rowid, COALESCE(LENGTH(data), 0) + 128 FROM features ORDER BY last_seen"
                        ):
                            if excess <= 0:
                                break
                            doomed.append((rowid,))
                            excess -= size
                        self._conn.executemany("DELETE FROM features WHERE rowid=?", doomed)
                        features_removed += len(doomed)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return GcResult(files_removed, features_removed)

    def stats(self) -> dict:
        """Row counts and the database size, for display."""
        files = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        features = self._conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return {"path": str(self.path), "files": files, "features": features, "bytes": size}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class NullCache:
    """A cache that remembers nothing. Pass ``cache=False`` to a scan to use it."""

    path = None

    def content_hash(self, uri, size, mtime_ns, validator=None):
        return None

    def feature(self, content_hash, key):
        return False, None

    def reader(self):
        return self

    def put_hashes(self, rows):
        pass

    def put_features(self, rows):
        pass

    def touch(self, uris=(), features=()):
        pass

    def close(self):
        pass
