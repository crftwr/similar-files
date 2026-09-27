# Playlist format

similar-files writes one playlist per group. Any program that reads M3U can
open one; programs that want the similarities read the `#SIMILAR-FILES-…`
lines described here. This format is a contract: a change that breaks a
reader bumps `format`.

## File

- One `.m3u8` file per group, in the output folder.
- UTF-8, `\n` line endings, no byte order mark. A POSIX filename that is not
  valid UTF-8 is written as its original bytes, so the line still opens the
  file.
- Named `<anchor file name> (<member count>).m3u8`, for example
  `IMG_0012.jpg (3).m3u8`. Characters invalid on Windows, macOS or Linux
  become `_`, and reserved Windows names (`CON`, `NUL`, …) get a leading `_`.
  A name that clashes (case-insensitively) with another playlist or with a
  file already in the folder gets ` [2]`, ` [3]`, … before `.m3u8`.

## Lines

```
#EXTM3U
#SIMILAR-FILES-GROUP:{"format":1,"members":2,"method":"image","params":{"algorithm":"phash","hash_size":8},"threshold":0.8,"version":1}
#SIMILAR-FILES-ITEM:{"role":"anchor","similarity":1.0}
/Users/me/Pictures/IMG_0012.jpg
#SIMILAR-FILES-ITEM:{"role":"member","similarity":0.9688}
/Users/me/Pictures/2019/IMG_0012 copy.jpg
#SIMILAR-FILES-ITEM:{"role":"member","similarity":0.875}
/Volumes/Backup/IMG_0012_small.jpg
```

1. `#EXTM3U`, always the first line.
2. `#SIMILAR-FILES-GROUP:` and a JSON object about the whole group, always
   the second line:

   | Key | Meaning |
   |---|---|
   | `format` | This format's version, currently `1`. |
   | `method` | `"identical"`, or the name of the extractor that compared the files (`"image"`). |
   | `version` | The extractor's version (`0` for identical). |
   | `params` | The extractor's parameters (`{}` for identical). |
   | `threshold` | The similarity, 0–1, every member reached. |
   | `members` | The number of members, not counting the anchor. |

3. Then one entry per file, **anchor first**, then members nearest first.
   Each entry is a `#SIMILAR-FILES-ITEM:` line with a JSON object, then the
   path line:

   | Key | Meaning |
   |---|---|
   | `role` | `"anchor"` or `"member"`. |
   | `similarity` | Similarity to the anchor, 0–1, rounded to 4 places. `1.0` is identical. |

Path lines are **absolute paths** for local files and **URIs** for remote
ones (`ssh://…`, `s3://…`), exactly as the item's `uri`.

## Reading it

- Players ignore every `#` line, and so may any reader that only wants the
  files.
- A reader wanting the extras parses each `#SIMILAR-FILES-…:` line as JSON,
  and ignores keys it does not know: new keys may be added without a format
  bump.
- A file whose name contains a line break cannot be written to M3U; it is
  left out of its group and logged.
- `similar_files.read_playlist(path)` returns `(group, [(uri, item), …])`.
