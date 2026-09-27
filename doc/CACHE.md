# The cache

similar-files keeps what it computes (content hashes and features) in one
SQLite database per user. It is a **cache, not state**: deleting it loses
nothing but time. The schema is documented because other programs (XeFM)
share the file; they should use the library to read it, not SQL.

## Location

Decided in `similar_files.resolve_cache_path()`, in this order:

1. A path the caller passes (`--cache`, or `Cache(path)`).
2. The `SIMILAR_FILES_CACHE` environment variable.
3. The platform default, with the file `cache.sqlite3` in:
   - macOS: `~/Library/Caches/similar-files/`
   - Windows: `%LOCALAPPDATA%\similar-files\Cache\`
   - Linux and others: `$XDG_CACHE_HOME/similar-files/` (default `~/.cache/similar-files/`)

A path that names an existing folder, or ends with a separator, means
`cache.sqlite3` inside it.

similar-files refuses to open a SQLite file that holds other tables and no
similar-files schema, so a mistyped `--cache` never changes someone else's
database.

## Concurrency

WAL mode, a 30-second busy timeout, and every write in a short
`BEGIN IMMEDIATE` transaction. A CLI run and XeFM can scan at the same time.
Within one scan, only the scanning thread writes; worker threads read through
their own connections.

## Schema (version 1, in `PRAGMA user_version`)

```sql
CREATE TABLE files (
    uri          BLOB PRIMARY KEY,  -- the item's uri, UTF-8 with surrogateescape
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    validator    TEXT,              -- ETag or similar, NULL if the source has none
    content_hash TEXT NOT NULL,     -- "sha256:<hex>"
    last_seen    INTEGER NOT NULL   -- Unix time
);
CREATE TABLE features (
    content_hash      TEXT NOT NULL,
    extractor         TEXT NOT NULL,
    extractor_version INTEGER NOT NULL,
    params_digest     TEXT NOT NULL,  -- first 16 hex of sha256 over the sorted-key JSON of the parameters
    data              BLOB,           -- the extractor's encoding; NULL = it could not read the file
    last_seen         INTEGER NOT NULL,
    PRIMARY KEY (content_hash, extractor, extractor_version, params_digest)
);
```

- `files` is only a shortcut: while `(uri, size, mtime_ns, validator)`
  matches, the file is not read again. Two spellings of one path just cost a
  second hash.
- `features` is keyed by content, so a moved or copied file reuses its
  features, and duplicates are extracted once. A different parameter value
  is a different row, so switching back is free.
- An older schema is dropped and recreated. A newer one is refused
  (`CacheError`): use another `--cache`.

## Garbage collection

`Cache.gc(max_age_days=…, max_bytes=…)`, or
`similar-files cache gc --max-age-days N --max-size-mb M`: rows not seen for
the age are removed, then the oldest features until the size fits. Nothing
is removed because its file does not exist right now: an unplugged drive is
not a deleted file.
