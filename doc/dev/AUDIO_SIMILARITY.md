# Audio similarity: Chromaprint fingerprints aligned by offset

The `audio` extractor (`similar_files/audio.py`) finds:

- the same recording re-encoded, or with its tags changed;
- an excerpt cut out of a longer recording;
- a track inside a longer mix, or a version with an intro or ending added.

Until 2026-10 it fingerprinted the first 120 s of each file, compared two
fingerprints only at shifts of up to ±1.5 s, and rejected any pair whose
durations differed by more than 3 s and 10%. It found re-encoded copies and
nothing else. The extractor's `version` stayed 1: the feature for a given
`length` is the same bytes as before, only the default `length` and the
comparison changed (see "Cache" below).

## What a similarity means

The similarity of two recordings is **the share of the shorter one found in
the other**, with both lined up at the best offset, the way
[video](VIDEO_SIMILARITY.md) measures it. "Shorter" means the fingerprint
with fewer items.

| Pair | Similarity |
|---|---|
| A re-encoded copy | about 1 |
| An excerpt and the recording it was cut from | about 0.9 |
| A recording, and the same with a 20 s intro added | about 1 |
| Two recordings, each half made of the other | about 0.5 |
| Unrelated audio | 0 |

It is containment, not equality, and symmetric. At the default threshold of
0.7, a recording groups with one that contains it and with one it is
contained in. The grouping picks the largest file as the anchor first,
usually the longest recording, so a group reads "this mix, and the tracks
in it".

## Extraction

```text
fpcalc -raw -json -length LENGTH FILE
```

`length` 0, the default, fingerprints the whole file. One item covers about
0.124 s, so a 4-minute track has about 1,900 items (7.6 KB) and an hour-long
mix about 29,000 (116 KB). The feature is a header (duration, item count)
and the items as little-endian 32-bit words.

A file is decoded in full: a one-hour MP3 takes 2.7 s, against 0.12 s for
its first 120 s (Apple silicon, fpcalc 1.6.2). One run may take up to 600 s
before it counts as a transient failure.

## Comparison

### 1. Candidate offsets, from items that match exactly

A re-encoded copy keeps many fingerprint items bit for bit: in the test
recordings, 98% for a 48 kbit/s MP3, and 21% for a 96 kbit/s AAC excerpt
cut between two item boundaries. `prepare()` indexes the items, and a query
looks up each of its own. Every match votes for an offset (item `i` of the
query on item `j` of the other votes for `j - i`).

- **Sampling.** An item is indexed only when the top 2 bits of
  `value × 0x9E3779B1` (mod 2³²) are zero: 1 in 4. The choice depends on
  the value alone, so two copies index the same items.
- **Repeats.** A value found more than 4 times in one fingerprint (silence,
  a held note) votes for many offsets at once, and is not indexed.
- **Common values.** A value found in more than 50 files (a jingle, a
  station ident) is dropped from the index.
- **Storage.** Each indexed item is one 64-bit word, `value << 32 |
  position`, where a position counts items across all fingerprints one
  after another. The words sit in 256 sorted arrays by the value's top
  byte, and a lookup bisects one of them.

### 2. Scoring an offset

The 3 offsets with the most votes are compared in full, each with its two
neighbours: a copy that starts a fraction of an item later splits its votes
between two offsets. Fewer than 2 votes for an offset and its neighbours is
chance, and is not compared.

At an offset, each item of the shorter fingerprint scores `1 - 2 × bit
error rate` against the item it lines up with, and 0 where the other has no
item. The similarity is the mean over the shorter fingerprint, clamped at
0. Over the overlap, unrelated audio differs in about half the bits and
scores about 0, so a part the two do not share neither helps nor hurts.

The XOR runs on each fingerprint held as one Python integer, so a whole
overlap is compared in one operation.

## Parameters and constants

| Name | Value | |
|---|---|---|
| `length` (parameter) | 0 (whole file) | Or at least 10 s. Part of the cache key. |
| default threshold | 0.7 | Below the "half contained" case (about 0.5) with a margin. |
| sampling | 1 in 4 | |
| repeats in one fingerprint | > 4 | Not indexed. |
| common value | > 50 files | Not indexed. |
| minimum votes | 2 | For an offset and its neighbours together. |
| offsets compared | 3 | With their neighbours. |

The comparison constants do not change the feature, so they need no
`version` bump.

## Cache

The cache key is `(extractor, version, params_digest)`. Changing the
default `length` from 120 to 0 changed the digest, so a scan with the
defaults extracts every file again, and the features cached under
`length=120` are no longer read. They are not deleted or overwritten: they
stay until the cache's garbage collection removes them by age, and a scan
with `--param length=120` still uses them, with the new comparison. With
`length=120`, only the first 120 s are compared, so two recordings that
start alike look contained in each other.

## Speed

Comparison, pure Python, synthetic features of 1,940 items (a 4-minute
track) each, 2026-10-26:

| Files | `prepare` | index memory | per query |
|---:|---:|---:|---:|
| 10,000 | 7.3 s | 125 MB with the fingerprints | 1.3 ms |

A first version kept the index in a dict of tuples: 1.3 GB for the same
10,000 files.

## Known limits

- **A changed speed or pitch** (a sped-up edit, PAL speed-up) changes the
  fingerprint items, and does not match.
- **A cover, a live version or a remix** is a different recording, and does
  not match.
- **Only the best single offset counts.** A medley that reorders parts of a
  track scores its largest part.
- **Audio that is silent or constant throughout** has no indexed items, and
  matches nothing.
- **A very short excerpt** (a few seconds) may have fewer than 2 exact item
  matches after a lossy re-encode, and then scores 0.
