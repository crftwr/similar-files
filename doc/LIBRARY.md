# Using similar-files from Python

The command line is a thin wrapper around these calls. Everything listed in
`similar_files.__all__` is the public API.

## Identical files

```python
import similar_files as sf

result = sf.find_identical(sf.walk(["/Users/me/Pictures"]))
for group in result.groups:            # largest first
    print(group.anchor.uri, [m.item.uri for m in group.members])
sf.write_playlists(result.groups, "/Users/me/similar-groups")
```

## Similar images

```python
fs = sf.extract_features(sf.walk(roots), "image")   # the slow part, cached
groups = sf.group_features(fs, threshold=0.9)       # instant
groups = sf.group_features(fs, threshold=0.75)      # regroup, no re-extraction
```

`sf.find_similar(items, "image", threshold=…)` does both in one call.
Thresholds are similarities, 0–1, for every extractor; each extractor has a
default (`image`: 0.8).

## Reference mode

Pass `references=[...]` (items, for example `sf.LocalFile.from_path(p)`).
Each reference that has matches anchors a group of them. References are
never members, and the scan never compares them with each other.

## Running on a worker thread

Every scan function takes:

- `cancel`: a callable returning `True` to stop. The scan stops handing out
  files and returns with `result.cancelled = True`, keeping the groups
  finished so far. Everything written to the cache stays valid.
- `progress`: called with `Progress(phase, done, total)`.
- `on_group` (identical files): called as each group completes.

All callbacks run on the thread that called the scan. The library never
prints and never exits; problems are logged under the `similar_files`
logger and counted in `result.errors`, `result.unreadable` and
`result.skipped_remote`.

`cache` takes a `sf.Cache` (kept open, for a long-lived caller), a path,
`None` for the per-user default, or `False` for no cache.

## Files from elsewhere: the item protocol

A scan takes any iterable of objects with these attributes (see
`sf.FileItem`):

| Attribute | |
|---|---|
| `uri: str` | Stable. The cache key and the playlist line. |
| `size: int`, `mtime_ns: int` | From the listing. |
| `validator: str \| None` | An ETag or similar, if the source has one. |
| `is_remote: bool` | Whether reading the content costs a network transfer. |
| `open()` | A binary file object. Called only when content is needed. |
| `content_hash()` | `"sha256:<hex>"` or `"md5:<hex>"` if the source can hash without sending the bytes, else `None`. |

For example, over a file manager's own path objects:

```python
class RemoteItem:
    def __init__(self, path):
        st = path.stat()
        self.uri, self.size, self.mtime_ns = str(path), st.size, st.mtime_ns
        self.validator, self.is_remote = getattr(st, "etag", None), True
        self._path = path

    def open(self):
        return self._path.open("rb")

    def content_hash(self):
        return None   # or "sha256:…" from `sha256sum` on the host
```

Rules worth knowing:

- Identical files are compared by size first, so only same-size files are
  ever read. When every file in a size group supplies a hash in the same
  algorithm, none is read at all. With mixed algorithms, similar-files reads
  the files and hashes them with SHA-256.
- A multipart S3 ETag (`…-N`) is not a content hash: return `None` for it.
- Perceptual extraction skips remote items unless `include_remote=True`,
  and counts them in `result.skipped_remote`.

## Writing an extractor

Subclass `sf.Extractor`, set `name`, `version`, `extensions`, `requires`,
`extra`, `default_params` and `default_threshold`, implement
`extract(stream) -> bytes` and `similarity(a, b) -> float` (0–1), and
decorate the class with `@sf.register`. Import optional packages inside
`extract`, never at module top level. Raise `sf.ExtractionFailed` for a file
you recognize but cannot read. Bump `version` whenever the output changes.
For speed over many files, override `prepare()` and `similarities()`.
