"""Dump what the cache has recorded, for debugging.

One line per file the cache knows, and one per feature extracted for its
content. A file with no feature yet (hashed for identical files only) shows
"-" as its extractor. Features whose content no known file has any more
(the file changed or was garbage-collected) are listed at the end.

    python tools/dump_cache.py
    python tools/dump_cache.py --extractor video --unreadable
    python tools/dump_cache.py --match /Volumes/Videos --cache temp/c.sqlite3

The status column: "ok" (feature stored), "unreadable" (the extractor could
not read the content; not tried again until the file changes), "hashed"
(content hash only). Only reads the database; never writes to it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from similar_files.cache import resolve_cache_path  # noqa: E402


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

    n_files, = conn.execute("SELECT COUNT(*) FROM files").fetchone()
    print(f"# {path}  ({os.path.getsize(path) / 1e6:.1f} MB, {n_files} file(s))")
    for name, version, digest, n, bad, size in conn.execute(
        "SELECT extractor, extractor_version, params_digest, COUNT(*), SUM(data IS NULL), "
        "SUM(COALESCE(LENGTH(data), 0)) FROM features GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ):
        print(f"#   {name} v{version} {digest}: {n} feature(s), {bad} unreadable, {size / 1e3:.1f} kB")
    print("\t".join(["status", "extractor", "bytes", "size", "modified", "last_seen", "content_hash", "uri"]))

    where, params = ["1"], []
    if args.extractor:
        where.append("x.extractor = ?")
        params.append(args.extractor)
    if args.unreadable:
        where.append("x.content_hash IS NOT NULL AND x.data IS NULL")
    # Files with no feature yet only belong in an unfiltered dump.
    join = "JOIN" if args.extractor or args.unreadable else "LEFT JOIN"
    rows = conn.execute(
        f"SELECT f.uri, f.size, f.mtime_ns, f.content_hash, f.last_seen, x.extractor, x.extractor_version, "
        f"x.content_hash IS NOT NULL, LENGTH(x.data) FROM files f {join} features x "
        f"ON x.content_hash = f.content_hash WHERE {' AND '.join(where)} ORDER BY f.uri, x.extractor",
        params,
    )
    shown = 0
    for raw_uri, size, mtime_ns, content_hash, last_seen, extractor, version, has_feature, data_len in rows:
        uri = uri_text(raw_uri)
        if args.match and args.match not in uri:
            continue
        if not has_feature:
            status, ex, nbytes = "hashed", "-", "-"
        else:
            status = "ok" if data_len is not None else "unreadable"
            ex, nbytes = f"{extractor} v{version}", str(data_len or 0)
        print("\t".join([
            status, ex, nbytes, str(size), when(mtime_ns // 1_000_000_000), when(last_seen),
            content_hash.split(":", 1)[-1][:12], uri,
        ]))
        shown += 1

    orphans = []
    if not args.match:
        where[0] = "NOT EXISTS (SELECT 1 FROM files f WHERE f.content_hash = x.content_hash)"
        orphans = conn.execute(
            f"SELECT x.extractor, x.extractor_version, LENGTH(x.data), x.last_seen, x.content_hash "
            f"FROM features x WHERE {' AND '.join(where)} ORDER BY x.extractor, x.last_seen",
            params,
        ).fetchall()
    for extractor, version, data_len, last_seen, content_hash in orphans:
        print("\t".join([
            "ok" if data_len is not None else "unreadable", f"{extractor} v{version}", str(data_len or 0),
            "-", "-", when(last_seen), content_hash.split(":", 1)[-1][:12], "(no known file)",
        ]))
    print(f"# {shown} row(s), {len(orphans)} feature(s) with no known file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
