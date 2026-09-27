# similar-files

Find files that are the same, or that look the same, and get each group as a
playlist you can open anywhere.

> **Status: early development.** Nothing is released yet. The commands below
> show the intended usage, not something you can install today.

## What it finds

- **Identical files**: the same bytes under different names or in different
  folders. Needs nothing beyond Python.
- **Similar images**: the same picture resized, re-encoded or lightly edited.
  Uses an optional dependency.

Similar video, similar audio and content-aware matching are planned for later.

## How it works

```bash
pip install similar-files            # identical files only
pip install "similar-files[image]"   # plus similar images

similar-files scan ~/Pictures --out ~/similar-groups
```

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

## Using it from Python

The command line is a thin wrapper. The scanning, caching and grouping are a
library that other programs can call. XeFM will be the first program to use
it.

## License

MIT. See [LICENSE](LICENSE).
