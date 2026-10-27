# similar-files

Find files that are the same, or that look the same, and get each group as a
playlist you can open anywhere.

> **Status: early development.** The command line and the library may still
> change between releases.

## What it finds

- **Identical files**: the same bytes under different names or in different
  folders. Needs nothing beyond Python.
- **Similar images**: the same picture resized, re-encoded or lightly edited.
  Uses an optional dependency.
- **Similar videos**: the same video re-encoded, resized or re-muxed, and
  clips cut out of a longer video. Needs [ffmpeg](https://ffmpeg.org) on your
  `PATH`.
- **Similar audio**: the same recording re-encoded or re-tagged, an excerpt
  and the recording it was cut from, a track inside a longer mix.
  Needs `fpcalc` from [Chromaprint](https://acoustid.org/chromaprint) on your
  `PATH` (`brew install chromaprint`, `apt install libchromaprint-tools`).

Content-aware matching ("screenshots of receipts") is planned for later.

## How it works

```bash
pip install similar-files            # identical files only
pip install "similar-files[image]"   # plus similar images (needs only Pillow)

similar-files scan ~/Pictures --out ~/similar-groups              # identical files
similar-files scan ~/Pictures --out ~/similar-images -m image     # similar images
similar-files scan ~/Movies --out ~/similar-videos -m video       # similar videos
similar-files scan ~/Music --out ~/similar-music -m audio         # similar audio
similar-files scan ~/Pictures --out ~/copies -r ~/Desktop/logo.png  # copies of one file
```

From a checkout, run `python -m similar_files` instead of `similar-files`.
`--threshold` (0–1) sets how close a match must be, and
`similar-files extractors` lists what can be compared.

Each group becomes one `.m3u8` playlist in the output folder, and its first
line is the file the others were compared with:

```
~/similar-groups/
  IMG_0012.jpg (3).m3u8
  logo_final.png (2).m3u8
```

M3U is a plain, widely supported list format, so you review a group in
whatever tool you like: a media player,
[XeFM](https://github.com/crftwr/xefm) (press Enter on a playlist to see its
files in a pane and copy, move or delete them there), or a text editor.

Two guarantees:

- **similar-files never deletes, moves or changes your files.** It only reads
  them, and you decide what to keep.
- **It writes nothing next to your files.** Features it computes are cached in
  your user cache folder, so a second scan is fast. You can delete that cache
  at any time.

## Watching a scan from another program

A long scan does not need anyone to stay attached to it. Start it with
`--status FILE`, and it keeps its progress in that file as JSON (replaced
whole each time, and rewritten every few seconds while it runs). Create the
file named by `--stop-file FILE` to stop it the way Ctrl-C does: the groups
found so far are still written.

```bash
similar-files scan ~/Movies --out ~/groups -m video \
    --status ~/scan/status.json --stop-file ~/scan/stop
```

Add `--format tree` and `--out` names one `.xft` file instead of a folder.
It holds every group, and it is rewritten every few minutes while the scan
runs, so you can start looking before the scan ends. Those intermediate
groups are provisional: a file read later can still join one, or start a
new group. The last write is final. XeFM opens this file the way it opens an
archive.

XeFM starts scans this way, so they keep running after you close it. See
[doc/STATUS_FORMAT.md](https://github.com/crftwr/similar-files/blob/main/doc/STATUS_FORMAT.md).

## Using it from Python

The command line is a thin wrapper. The scanning, caching and grouping are a
library that other programs can call, and XeFM is the first program to use
it. See [doc/LIBRARY.md](https://github.com/crftwr/similar-files/blob/main/doc/LIBRARY.md), and for the formats other programs
read, [doc/PLAYLIST_FORMAT.md](https://github.com/crftwr/similar-files/blob/main/doc/PLAYLIST_FORMAT.md) and
[doc/CACHE.md](https://github.com/crftwr/similar-files/blob/main/doc/CACHE.md).

## License

MIT. See [LICENSE](https://github.com/crftwr/similar-files/blob/main/LICENSE).
