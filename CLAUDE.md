# similar-files — Claude Code Instructions

similar-files finds files that are the same, or look or sound the same, and
writes each group it finds as a playlist. It is a **library first and a CLI
second**: the command line is a thin wrapper, and other programs import the
package directly.

Its first user is **[XeFM](https://github.com/crftwr/xefm)**, a dual-pane file
manager. The design comes out of XeFM discussion
[#474](https://github.com/crftwr/xefm/discussions/474); read that thread
before changing anything in this document. XeFM does not show the results in
a UI of its own. It opens each group's `.m3u8` with Enter (xefm#504), and
every file operation works on the list it shows. Anything else that reads
M3U — a media player, another file manager — can use the output too.

The package is `similar_files/` at the repo root (flat layout, as XeFM and
PuiKit have it). Tests live in `test/`, docs in `doc/`.

---

## What "similar" means: three layers

1. **Identical** — same bytes. No third-party dependencies. Ships first.
2. **Perceptual** — the same picture or sound after resizing or re-encoding:
   perceptual hashes for images (`imagehash`), frames for video (ffmpeg), and
   audio fingerprints (Chromaprint). Each is an **optional dependency**:
   supported, never required.
3. **Semantic** — "the same scene", "screenshots of receipts": embeddings.
   Later, heavy, and strictly opt-in.

**Current scope: layer 1 and layer 2 for images.** Video, audio and layer 3
come later. Don't build them ahead of time.

### Outlook: beyond images, video and audio

None of these is planned work. They are here because today's design must not
rule them out. See "What the registry must allow" below.

| Kind | What "similar" means | Likely feature |
|---|---|---|
| **Folders** | Two backup copies of the same tree, one of them slightly out of date | The set of content hashes under each folder; similarity = the share of files both have |
| **Archives** | `photos.zip` vs `photos.7z` vs the extracted folder | The set of member content hashes (read from the archive index where it has one) |
| **Text and documents** | `report_v2.docx` vs `report_final.docx`, a PDF and its source | Extracted text → shingles → MinHash / SimHash |
| **Source code** | A copied project folder that has drifted | Tokens → winnowing fingerprints (the approach of MOSS) |
| **Scanned pages** | The same page scanned twice, or a PDF of a scan | Render each page → perceptual image hash |
| **Photos by circumstance** | Burst shots and near-identical takes | EXIF capture time and GPS as the feature, combined with the image hash |
| **Music by tags** | The same track from two albums or rips | Normalized artist, title and duration, as a cheap pre-filter before audio fingerprints |
| **Fonts, e-books, subtitles** | The same font under two names; the same book as EPUB and PDF | Name tables; extracted text |
| **Semantic search** | "Screenshots of receipts" | The layer-3 embeddings queried by text instead of by a file. This is search rather than grouping, but it is the same index |

#### What the registry must allow

What the rows above need, and the first design must not close off:

- **Features are not always fixed-length bit vectors.** Sets (folders,
  archives), MinHash signatures, variable-length fingerprints and small
  records (EXIF, tags) all need to fit. The extractor owns the feature's
  encoding; the cache stores opaque bytes, typed by extractor.
- **A distance belongs to its extractor.** Hamming, Jaccard, cosine and
  domain-specific ones all occur. Grouping only needs "distance to the
  anchor" and "within the threshold".
- **An item can be a directory**, not only a file. A group of folders is a
  valid playlist line; XeFM shows directories in a list too.
- **Features can be derived from other features.** A folder's feature is
  built from its files' content hashes, which are already cached, so it costs
  no second read.
- **Cheap pre-filters combine with expensive features**, the way size gates
  hashing in layer 1: tags before audio fingerprints, page count before page
  hashes.

---

## Design principles

These are decisions, not suggestions. Changing one is a design discussion, not
a refactor.

### Library first

- Every capability is a function or class that another program can call. The
  CLI only parses arguments, calls the library, and prints.
- Library code **never prints** and never calls `sys.exit`. It reports through
  return values, exceptions, a progress callback, and `logging`.
- Long operations accept a **cancel check** and a **progress callback**, and
  are safe to run on a worker thread. XeFM will drive them from a background
  job.
- Keep the public API small and deliberate. XeFM will depend on it.

### A registry of extractors

One pattern covers every layer: **media kind → feature extractor → distance
function**. Layers 2 and 3 are *entries* in the registry, not code paths.

- An extractor declares a stable `name`, a `version`, the files it handles
  (by extension or sniffing), and the dependency it needs.
- Its `version` is part of the cache key. Bump it whenever its output changes,
  and the old features stop matching.
- An extractor whose dependency is missing registers as *unavailable*, with
  the `pip install similar-files[extra]` line that fixes it. It never breaks
  import.
- Third parties can register their own.

### Grouping is star-shaped

Similarity is **not transitive**: A≈B and B≈C does not mean A≈C. Merging by
connected components (union-find) chains unrelated files into one huge group.
Don't do it.

- A group is `anchor + members`. Each member is within the threshold **of the
  anchor**, and carries its distance to the anchor.
- **Discovery mode:** the grouping picks the anchors.
- **Reference mode:** the user names the anchors (several reference files),
  and the scan searches a separate set of candidates. References are never
  candidates for each other.
- A file may belong to more than one group. Each group is its own playlist, so
  nothing collides.
- Changing the threshold means **re-grouping from cached features**, never
  re-extracting.

### Identical files: cheapest test first

Group by size → hash a head-and-tail sample → hash the whole file, only for
the candidates still left. Process the **largest size buckets first**: the
groups that free the most space come out first, and results can stream.

### The cache

A SQLite database, and a **cache, not state**: deleting it loses nothing but
time.

- **Location: one per user, shared by every caller.** The default is the
  platform's user cache directory: `~/Library/Caches/similar-files/` on macOS,
  `%LOCALAPPDATA%\similar-files\Cache\` on Windows, and
  `$XDG_CACHE_HOME/similar-files/` (default `~/.cache/similar-files/`) on Linux.
  An argument (`--cache`) and an environment variable
  (`SIMILAR_FILES_CACHE`) override it.
  - **XeFM uses the same default.** Features extracted by a CLI run are then
    reused when XeFM scans the same files, and the other way round. XeFM
    passes a path only if its user overrides it. The cache never goes in
    `~/.xefm/state.db`: that holds small state, not hundreds of megabytes of
    disposable features.
  - Under MSIX, XeFM's `%LOCALAPPDATA%` is virtualized, so the Store build gets
    a cache of its own. That is acceptable, because it is only a cache.
  - The location is decided in one function, so the rule is written once.
- **Features are keyed by content hash, not by path:**
  - `files(uri, size, mtime, content_hash, last_seen)` — when `(uri, size,
    mtime)` still matches, the file is not read again.
  - `features(content_hash, extractor, extractor_version, data)` — a moved or
    copied file reuses its features, and duplicates are extracted once.
- Hash the file in the same pass that extracts its features, so it is read
  once.
- Invalidate lazily on a stat mismatch. Garbage-collect by `last_seen` age and a
  size cap, **never by "the file does not exist right now"**. An unplugged
  drive or an unreachable server is not a deleted file, and its features
  should still be there when it comes back.
- Use WAL mode, and serialize writes, because a CLI run and XeFM may scan at
  the same time.
- The `files.uri` key is only a shortcut that skips re-reading a file. If two
  callers spell the same file differently (`/var/…` vs `/private/var/…`), the
  file is hashed again, and its features are still found by content hash.
  Canonicalize where it is cheap, and don't build correctness on it.
- **Never write anything next to the files being scanned.** The scanned
  folders may be read-only, remote, or simply not ours.

### Output: one playlist per group

- One `.m3u8` per group, in an output folder: UTF-8, a `#EXTM3U` header, and
  the **anchor on the first path line**, followed by members nearest first.
- Write **absolute** paths, or URIs for remote items (see below). The output
  folder is rarely next to the files it names.
- Name each file after its anchor plus the member count, as in
  `IMG_0012.jpg (3).m3u8`. Sanitize names for every OS, and make collisions
  unique.
- Distances and other extras go in `#` lines that players ignore. The exact
  syntax is specified in `doc/`, the first time one is written. XeFM skips
  `#` lines in playlists.
- CSV/JSON output for scripts may come later. The playlist stays the
  primary output.

### Remote files, and how XeFM hands them in

XeFM browses the local disk, network mounts, `ssh://` hosts and `s3://`
buckets. similar-files must work on all of them **without depending on
XeFM**, and without growing its own SSH or S3 client.

**Files come in through a small protocol, not as `pathlib` paths.** The
library defines what it needs from one file:

- `uri`: a stable string. It is the cache key and the playlist line.
- `size`, `mtime`, and an optional `etag` or other validator.
- `open()` to read bytes, used only when content is needed.
- `is_remote`: whether reading the content costs a network transfer.
- An optional `content_hash()` for a source that can produce one without
  sending the bytes (an S3 ETag, `sha256sum` run on the SSH host).

A local-path adapter ships in the library and is what the CLI uses. XeFM
writes an adapter over its own `Path` objects, so `ssh://` and `s3://` come in
through XeFM's existing backends, credentials and connection pooling. The
library never imports XeFM, and never sees a password.

**Cost rules for remote files:**

- **Identical (layer 1)** runs anywhere. Size grouping needs only the listing,
  so only same-size candidates are ever read, and a source that can
  `content_hash()` sends no content at all.
- An **S3 ETag** equals the MD5 only for single-part uploads; a multipart ETag
  (`…-N`) is not a content hash. That knowledge belongs in XeFM's adapter,
  which says "no hash" rather than returning a wrong one.
- **Perceptual layers on remote files are opt-in.** Extracting features means
  downloading the whole file. The default policy skips `is_remote` files for
  layers 2 and 3, and says how many it skipped.
- **Network mounts (SMB, NFS, AFP) look local and are not.** The
  local-path adapter cannot always tell. The caller can mark a root as remote,
  and XeFM knows which paths are its own net mounts.
- The cache is what makes remote scans affordable: a feature computed once is
  never downloaded for again while `(uri, size, mtime/etag)` still matches.

**Output for remote files.** A playlist line is the item's `uri`, so a group
on an SSH host is written as `ssh://…` lines. XeFM opens those URIs in a list;
a media player does not. That is expected, and not worth working around.

### Hands off the files

similar-files **reads**. It never deletes, moves, renames or modifies a
scanned file, and it has no flag to do so. Deciding what to keep and deleting
it is the user's job, in XeFM or wherever the user chooses. A future "suggest
what to keep" feature may produce a *selection*, never an action.

---

## Dependencies

- **Core: standard library only.** Layer 1 must work with a bare
  `pip install similar-files`.
- Optional features go in extras: `[image]` for Pillow and `imagehash`,
  `numpy` where vector math needs it, and later `[video]` and `[audio]`.
  Import an optional package **inside** the extractor or function that needs
  it, never at module top level.
- An approximate-nearest-neighbour index (faiss, hnswlib) is not a starting
  dependency. Numpy, or even pure Python over 64-bit hashes, is enough for
  tens of thousands of images. Add an index only when a measured case needs
  one.
- Don't depend on XeFM or PuiKit. This project stands alone; XeFM depends on
  it, not the other way round.

---

## Terminal session rules

### Virtual environment

- The venv lives at `.venv/`. Assume it is active in ongoing sessions; activate
  it only when starting fresh.
- If you do activate, run it as a separate command, not chained with `&&`:
  ```bash
  source .venv/bin/activate
  python -m pytest
  ```

### Running things

Run everything **from the repo root**. `python -m` puts the root on
`sys.path`, so the flat `similar_files` package resolves without installing:

```bash
python -m pytest test/ -v
python -m similar_files --help
```

### Git

- Use `--no-pager` for any git command that may page output: `diff`, `log`,
  `show`, `branch`, `tag`, `blame`, `grep`.
- Commit and push only when the user asks. Work on a branch and open a PR;
  don't commit to `main`.

### Scans on real folders

Don't point a scan at the user's real photo or media folders unless asked. A
scan can read gigabytes and fill the cache. Test on generated data in a
temporary directory.

---

## Coding standards

### Logging

- Library modules: `logger = logging.getLogger(__name__)`. Never configure
  handlers or levels in library code; that is the caller's job.
- `print()` is allowed only in `similar_files/cli.py`, for output the user
  asked for. Diagnostics go through `logging` there too.
- Levels: `error` for failures and data loss; `warning` for degraded behavior
  (a file skipped, an extractor unavailable); `info` for normal progress;
  `debug` rarely.

### Errors

- Catch specific exceptions. One unreadable file never aborts a scan: log it,
  count it, and report the count at the end.
- When catching `Exception`, log it with the path it happened on.

### Paths and text

- Filenames are bytes on POSIX. Don't normalize a path you will open again;
  NFC is only for comparing or displaying.
- Read and write text as UTF-8, explicitly. Never rely on the locale default,
  because Windows differs.

### Style

- Type hints on public functions. Docstrings say what a function is for and
  what the caller can rely on.
- Before adding an import, check whether the module already imports it at the
  top of the file.
- Python files are not executable; run them with `python`.

---

## Tests

- `test/test_*.py`, run with `pytest`.
- **Generate fixtures; don't commit media.** Make images with Pillow inside
  the test (draw one, then resize, recompress, or crop it to make its similar
  copies), and write byte-identical files to a temp directory.
- Every extractor gets a test proving that a near-copy groups with its
  original, and that a different image does not.
- A test that needs an optional dependency skips cleanly when the dependency
  is missing (`pytest.importorskip`). The core suite must pass on a bare
  install.

---

## File placement

| What | Where | Naming |
|---|---|---|
| Package | `similar_files/` | `*.py`, imported as `similar_files.<module>` |
| CLI entry | `similar_files/cli.py`, `similar_files/__main__.py` | |
| Tests | `test/` | `test_*.py` |
| User docs | `doc/` | `FEATURE_NAME.md` |
| Developer docs | `doc/dev/` | `SYSTEM_NAME.md` |
| Dev tools | `tools/` | |
| Throwaway files | `temp/` (git-ignored) | `temp_*` |

Write docs when the user asks, or when a change clearly needs them. The
output format and the cache schema are contracts other programs depend on:
document them in `doc/` when they are first implemented.
