"""Dump what the cache has recorded, for debugging.

One line per feature (keyed by file: URI, size and mtime), then one line
per file whose content hash is known (for identical files), marked
"hashed". A feature whose file has changed since stays until it is
extracted again or garbage-collected; its size and modified columns are the
ones it was extracted at.

    python tools/dump_cache.py
    python tools/dump_cache.py --extractor video --unreadable
    python tools/dump_cache.py --match /Volumes/Videos --cache temp/c.sqlite3

The status column: "ok" (feature stored), "unreadable" (the extractor could
not read the file; not tried again until it changes), "hashed" (content
hash only). Only reads the database; never writes to it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from similar_files.cache import SCHEMA_VERSION, resolve_cache_path  # noqa: E402


def when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def uri_text(raw) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", help="cache database (default: the per-user cache, as the CLI finds it)")
    ap.add_argument("--extractor", help="only features of this extractor (image, video, audio…)")
    ap.add_argument("--match", help="only files whose URI contains this text")
    ap.add_argument("--unreadable", action="store_true", help="only features recorded as unreadable")
    args = ap.parse_args()

    path = resolve_cache_path(args.cache)
    if not path.is_file():
        print(f"no cache at {path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    version, = conn.execute("PRAGMA user_version").fetchone()
    if version != SCHEMA_VERSION:
        print(f"{path} has schema {version}, not {SCHEMA_VERSION}; the next scan with this version updates it",
              file=sys.stderr)
        return 1

    n_files, = conn.execute("SELECT COUNT(*) FROM files").fetchone()
    print(f"# {path}  ({os.path.getsize(path) / 1e6:.1f} MB, {n_files} hashed file(s))")
    for name, version, digest, n, bad, size in conn.execute(
        "SELECT extractor, extractor_version, params_digest, COUNT(*), SUM(data IS NULL), "
        "SUM(COALESCE(LENGTH(data), 0)) FROM features GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ):
        print(f"#   {name} v{version} {digest}: {n} feature(s), {bad} unreadable, {size / 1e3:.1f} kB")
    print("\t".join(["status", "extractor", "bytes", "size", "modified", "last_seen", "content_hash", "uri"]))

    def row(status, ex, nbytes, size, mtime_ns, last_seen, content_hash, raw_uri):
        print("\t".join([
            status, ex, nbytes, str(size), when(mtime_ns // 1_000_000_000), when(last_seen),
            content_hash.split(":", 1)[-1][:12] if content_hash else "-", uri_text(raw_uri),
        ]))

    where, params = ["1"], []
    if args.extractor:
        where.append("extractor = ?")
        params.append(args.extractor)
    if args.unreadable:
        where.append("data IS NULL")
    shown = 0
    for raw_uri, size, mtime_ns, last_seen, extractor, version, data_len in conn.execute(
        "SELECT uri, size, mtime_ns, last_seen, extractor, extractor_version, LENGTH(data) FROM features "
        f"WHERE {' AND '.join(where)} ORDER BY uri, extractor, extractor_version",
        params,
    ):
        if args.match and args.match not in uri_text(raw_uri):
            continue
        status = "ok" if data_len is not None else "unreadable"
        row(status, f"{extractor} v{version}", str(data_len or 0), size, mtime_ns, last_seen, None, raw_uri)
        shown += 1

    # Content hashes only belong in a dump not filtered by feature.
    if not (args.extractor or args.unreadable):
        for raw_uri, size, mtime_ns, last_seen, content_hash in conn.execute(
            "SELECT uri, size, mtime_ns, last_seen, content_hash FROM files ORDER BY uri"
        ):
            if args.match and args.match not in uri_text(raw_uri):
                continue
            row("hashed", "-", "-", size, mtime_ns, last_seen, content_hash, raw_uri)
            shown += 1
    print(f"# {shown} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
