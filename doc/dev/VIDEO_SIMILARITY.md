# Video similarity: keyframes aligned by time offset

The `video` extractor (version 2, `similar_files/video.py`) finds:

- the same video re-encoded, resized or re-muxed;
- a clip cut out of a longer video;
- a video with an intro, an ending or other footage added around it.

It is built to be fast: one ffmpeg run per file, decoding keyframes only.

Version 1 sampled 10 frames at fixed shares of the running time, and
rejected any pair whose durations differed by more than 5%. It could not see
a clip in its source, and missed a copy with a few seconds added at the
start. Version 2 replaces it.

## What a similarity means

The similarity of two videos is **the share of the shorter one found in the
other**, with both lined up at the best time offset. "Shorter" means the
running time, not the number of keyframes: a longer video may well have
fewer keyframes (a longer GOP, more static shots), and measuring from it
would count its extra footage as a mismatch. So:

| Pair | Similarity |
|---|---|
| A re-encoded copy | about 1 |
| A 10-minute clip and the 1-hour film it was cut from | about 1 |
| A video, and the same video with a 30 s intro added | about 1 |
| A video half made of another one's footage | about 0.5 |
| Unrelated videos | 0 |

This is containment, not equality. A trailer and its film group together,
and so do two clips cut from the same film, if they overlap. The grouping
picks the largest file as the anchor first, which is usually the longest
video, so a group reads "this film, and the clips of it".

It follows that, at the default threshold of 0.7:

- a video is similar to one that **contains** it, and to one it **is
  contained in**: both score about 1, whichever is the anchor;
- two videos that **share a part** are similar when the part is at least 70%
  of the shorter one. Two hour-long videos sharing 10 minutes score about
  0.17. A lower `--threshold` finds such overlaps too, as long as the part
  gives at least 3 matching keyframes.

The measure is symmetric: it is always computed from the shorter video.

## Extraction

One ffmpeg run:

```text
ffmpeg -skip_frame nokey -i FILE -map 0:v:0
       -vf "select='isnan(prev_selected_t)+gte(t-prev_selected_t,INTERVAL)',
            scale=9:8:flags=area,format=gray,showinfo"
       -fps_mode passthrough -f rawvideo -
```

- `-skip_frame nokey` makes the decoder skip every frame that is not a
  keyframe. Nothing between keyframes is decoded, which is where version 1
  spent its time. A decoder that ignores the option still works, only
  slower.
- `select` keeps at most one keyframe per `interval` seconds (parameter,
  default 1.0). This matters for all-intra formats (MJPEG, ProRes, some
  screen recordings), where every frame is a keyframe.
- ffmpeg scales each frame to 9×8 grey pixels, and Python turns those into a
  64-bit difference hash. `showinfo` reports each frame's timestamp on
  stderr, in the same order as the pixels on stdout.
- The input summary on stderr (`Duration: …, start: …`) gives the running
  time. ffprobe is no longer needed.

Two kinds of frame are dropped:

- **Flat frames**, whose brightest and darkest pixels differ by less than 12
  (black, a fade, a plain background). Their hash is noise.
- A frame within 3 bits of the frame kept before it. A static shot or a
  slide then keeps one frame, not one per GOP.

The feature is `start, end` (float64 seconds), a frame count (uint32), and
per frame `time` (float32 seconds) and `hash` (uint64). That is 12 bytes a
frame: an hour at a 2 s GOP is about 20 KB.

### Keyframes differ between encodings

A re-encoded copy puts its keyframes wherever its encoder chose. Two things
make keyframes still comparable:

- **Scene cuts.** Every mainstream encoder (x264, x265, libvpx, hardware
  encoders) puts a keyframe at a cut. Both copies then have a keyframe at
  the same moment, showing the same picture.
- **Slow shots.** Within a shot, a frame half a GOP away is often the same
  picture to a 9×8 hash.

In a fast shot (a pan, sport, noise), keyframes a second apart are different
pictures. The comparison below is built so that this lowers nothing.

## Comparison

### 1. Candidate pairs, through a band index

Two frames match when their hashes are at most 10 bits apart. Comparing
every frame with every other is too slow, so each 64-bit hash is cut into
4 bands of 16 bits, and `prepare()` indexes every frame under each band
value. Only frames that share at least one band are compared.

That finds every pair within 3 bits (pigeonhole), and most within 10. The
same rule is used for a single pair (`similarity()`) and for a collection
(`similarities()`), so the two agree.

### 2. The time offset, by a sliding window

Each matching pair `(i, j)` has an offset `time_b[j] - time_a[i]`. With the
offsets sorted, a window 6 s wide (±3 s: a re-encoded copy's keyframes may
be up to a GOP apart) slides over them. The window holding the most distinct
frames of the shorter video wins, and those frames are **matched**.

Fewer than 3 matched frames is chance, and the similarity is 0. The
exception is a video with fewer than 3 frames in all, which must match all
of them.

Only the best offset is kept. A video made of pieces of another at several
offsets scores the largest piece.

### 3. What counts against

The best offset is the median of the matched pairs. At that offset, an
unmatched frame of the shorter video:

- **counts against** when it falls outside the other video's running time
  (the other video does not have that part), or when the other video has a
  keyframe within 0.25 s of it (the same moment, usually a scene cut) and
  that keyframe differs;
- **is left out** otherwise. The other video has no keyframe at that
  moment, so the two cannot be compared.

The similarity is `matched / (matched + against)`.

That is why a fast shot lowers nothing: its keyframes are left out unless
the other copy happens to have one at the same moment. It is also why a
clip scores 1 against its source, while a video half made of other footage
scores about 0.5. That footage is either outside the other's running time,
or disagrees with its keyframes at the same moments.

### 4. Different keyframe spacing

Nothing requires the two videos to have their keyframes at the same places,
or equally often. Take a copy with a keyframe every second against one with
a keyframe every 10 seconds:

- Scene cuts are keyframes in both, at the same moment. They match, or
  count against.
- In a slow shot, a keyframe of one matches the other's keyframe up to
  ±3 s away.
- A keyframe of the dense one with no keyframe of the sparse one near it,
  in a fast shot, is left out. It neither helps nor hurts.

So a different spacing does not lower the similarity. It lowers the
**number of frames that decide it**: the score rests on the moments the
sparse video has keyframes. That costs little on a long video. A short
clip against a video with sparse keyframes may have fewer than 3 frames to
compare, and then scores 0 even when those frames match exactly (see Known
limits).

Where every frame is a keyframe (all-intra formats), `interval` keeps one a
second, so the dense side is never denser than that.

### 5. Frames many videos share

A frame found in many videos (a channel's intro, a logo, a test card) tells
them apart no better than a flat frame. It would also make every query walk
a huge index bucket. `prepare()` counts, for each band value, the number of
videos that have it. A value in more than 50 videos, and in more than 8
times what chance would put there, is **common**. A frame with two or more
common band values is then left out of both sides of every comparison, like
a flat frame.

With fewer than 50 videos nothing is ever common, so `similarity()` of one
pair is not affected.

## Parameters and constants

| Name | Value | |
|---|---|---|
| `interval` (parameter) | 1.0 s | At most one keyframe per interval. Part of the cache key. |
| default threshold | 0.7 | Below the "half contained" case (about 0.5) with a margin. |
| match distance | 10 bits | Of 64. |
| offset tolerance | ±3 s | Covers keyframes a GOP apart. |
| same moment | 0.25 s | Keyframes this close must agree. |
| minimum matches | 3 | |
| flat range | 12 | Of 255. |
| same as previous | 3 bits | Drops repeats in static shots. |
| common band value | > 50 videos and > 8× chance | |

Changing anything that alters the extracted feature needs a `version` bump.
The comparison constants do not change the feature, so they need none:
features stay cached, and only the similarities change.

## Speed

Measured on 2026-09-27, Apple silicon, 10 cores, ffmpeg 9.0.2.

**Extraction**, `tools/bench_extract.py video --files 24` (H.264 1280×720,
20–26 s, default x264 GOP):

| workers | version 1 wall s | version 2 wall s |
|---:|---:|---:|
| 1 | 34.2 | 3.5 |
| 2 | 23.6 | 1.7 |
| 4 | 20.3 | 1.0 |
| 8 | 19.5 | 0.7 |

Version 1 decoded from the keyframe before each of its 10 sample points.
Version 2 decodes keyframes only, and still scales with the workers.

On one 10-minute 1920×1080 file (1.1 GB, 2 s GOP), version 2 takes 1.5 s.
Version 1 took 0.65 s there: with a short GOP, its 10 seeks were cheap.
Version 2 reads the whole file, so on a long video **the file's size, not
its decoding, sets the time**. On a network mount that is the transfer.
ffmpeg is the only reader: features are cached by file (URI, size, mtime),
not by a content hash, which would take a second full read. On a gigabit SMB
share that read runs at about 90 MB/s.
One run may take up to 600 s before it counts as a transient failure.

**Comparison**, pure Python (synthetic features, 300 keyframes each):

| Videos | `prepare` | per query | all queries (discovery) |
|---:|---:|---:|---:|
| 1000 | 1.4 s | 4.6 ms | 4.6 s |
| 3000 | 4.6 s | 15 ms | 45 s |
| 1000, 30% of frames from 50 shared ones | 2.4 s | 2.4 ms | 2.4 s |

The last row took 358 ms a query (6 minutes in all) before common frames
were left out.

Discovery costs O(N²) in the number of videos, through the index. For more
than a few thousand long videos, an index over whole videos, or numpy, is
the next step. Add either only when a real case needs it.

## First look at real videos

26 feature-length videos (27–134 min, H.264 from several studios) on an SMB
share, 2026-09-27:

- One keyframe every 4–10 s (0.10–0.24 a second), so 300–1,450 frames and
  4–17 KB a video. `interval` never applied.
- No pair reached the threshold. None was a copy of another, so this shows
  no false groups, not that copies are found.
- The highest scores, 0.06–0.10, were real: videos from one studio open with
  the same sequence, which matched in their first minute.
- One false match scored 0.08: 2–3 dark, simple frames near one video's end,
  at 7–10 bits, just inside the match distance.

## Known limits

- **Mirrored, cropped or letterboxed copies** do not match: the 9×8 hash
  sees a different picture.
- **A changed frame rate with a changed speed** (PAL speed-up, 25 vs 23.976)
  drifts the offset by about 4%. A long video then drifts out of the ±3 s
  window.
- **Too few keyframes to compare.** A match needs 3 aligned keyframes. A
  short clip (10 s) against a video with a long GOP (10 s) may find only its
  2 scene cuts there, and score 0 although both match exactly.
- **Only the best single offset counts.** A re-edit that reorders scenes
  scores its largest piece.
- **A video that is flat throughout** has no frames, and matches nothing.
