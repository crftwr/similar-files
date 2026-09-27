"""Layer 2 for video: keyframe hashes, aligned by time offset (ffmpeg).

Needs ``ffmpeg`` on ``PATH``, and no Python package. One ffmpeg run decodes
only the keyframes, scales each to the 9×8 grey picture a difference hash is
computed from, and reports its timestamp. Comparing two videos finds the time
offset at which most of their keyframes agree, so a clip cut out of a longer
video, or a video with an intro added, still matches. The design is in
``doc/dev/VIDEO_SIMILARITY.md``.
"""

from __future__ import annotations

import bisect
import logging
import re
import struct
from collections import defaultdict
from typing import Any, NamedTuple, Sequence

from .registry import ExtractionFailed, Extractor, register, run_program

logger = logging.getLogger(__name__)

#: One frame, scaled for a 64-bit difference hash: 9 columns, 8 rows, 1 byte each.
_W, _H = 9, 8
_FRAME_BYTES = _W * _H
#: A frame whose brightest and darkest pixels differ by less than this is
#: flat (a fade to black, a title card's background): its hash is noise.
_FLAT_RANGE = 12
#: A keyframe this close (in bits) to the one kept before it adds nothing
#: (a static shot, a slide): it is dropped.
_SAME_DISTANCE = 3
#: Two frame hashes at most this many bits apart are the same picture.
_MATCH_DISTANCE = 10
#: The hash is cut into this many bands; frames are compared only when at
#: least one band is equal. That finds every pair within 3 bits and most
#: within 10, without comparing every frame with every other.
_BANDS = 4
_BAND_BITS = 64 // _BANDS
_BAND_MASK = (1 << _BAND_BITS) - 1
#: Seconds either side of the best offset that still count as aligned. A
#: re-encoded copy puts its keyframes elsewhere, up to a GOP away.
_OFFSET_TOLERANCE = 3.0
#: Two keyframes this many seconds apart, once aligned, show the same moment
#: (a scene cut, where every encoder puts a keyframe) and must match.
_SAME_MOMENT = 0.25
#: Fewer aligned frames than this are chance, unless the shorter video has
#: fewer frames than this in all.
_MIN_MATCHES = 3
#: A band value in more videos than this, and than _COMMON_FACTOR times
#: what chance puts in one, marks a frame many videos share.
_COMMON_MIN_VIDEOS = 50
_COMMON_FACTOR = 8
_HEADER = struct.Struct("<ddI")
_FRAME = struct.Struct("<fQ")
#: Seconds an ffmpeg run may take before the file counts as unreadable. It
#: reads the whole file, which on a network mount takes a while.
_TIMEOUT = 600
_DURATION = re.compile(rb"Duration: (\d+):(\d+):([\d.]+), start: (-?[\d.]+)")
_PTS_TIME = re.compile(rb"^\[Parsed_showinfo[^\]]*\].*?\bpts_time:\s*(\S+)", re.MULTILINE)


class _Video(NamedTuple):
    start: float
    end: float
    #: ``(time, hash)`` of each kept keyframe, in time order.
    frames: list[tuple[float, int]]


def _span(stderr: bytes) -> tuple[float, float | None]:
    """``(start, end)`` in seconds from ffmpeg's input summary; ``end`` is ``None`` if unknown."""
    m = _DURATION.search(stderr)
    if not m:
        return 0.0, None
    h, mnt, sec, start = m.groups()
    start_s = float(start)
    return start_s, start_s + int(h) * 3600 + int(mnt) * 60 + float(sec)


def _dhash(pixels: bytes) -> int | None:
    """The hash of one 9×8 grey frame, or ``None`` if it is flat."""
    if max(pixels) - min(pixels) < _FLAT_RANGE:
        return None
    value = 0
    for row in range(_H):
        base = row * _W
        for col in range(_W - 1):
            value = (value << 1) | (pixels[base + col] < pixels[base + col + 1])
    return value


def _error_line(stderr: bytes) -> str:
    lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln and not ln.startswith("[Parsed_showinfo")]
    return lines[-1].strip() if lines else ""


@register
class VideoExtractor(Extractor):
    """Keyframes, each reduced to a 64-bit difference hash with its timestamp.

    Two videos are aligned by the time offset at which the most keyframes of
    the shorter one (running time) have a match in the other. The
    similarity is the share of the shorter one's keyframes matched at that
    offset: how much of it the other video contains. A re-encoded, resized
    or re-muxed copy scores near 1, and so does a clip cut out of a longer
    video, or a video with an intro or ending added.
    """

    name = "video"
    version = 2
    extensions = frozenset({
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
        ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv",
    })
    requires_programs = ("ffmpeg",)
    needs_path = True
    default_params = {"interval": 1.0}
    default_threshold = 0.7
    description = "Similar videos: copies, and clips cut out of longer ones (keyframe hashes, ffmpeg)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if not 0.1 <= params["interval"] <= 60:
            raise ValueError("interval must be 0.1–60 seconds")

    def extract_file(self, path: str) -> bytes:
        select = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{self.params['interval']})'"
        proc = run_program([
            "ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-nostdin",
            "-skip_frame", "nokey", "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"{select},scale={_W}:{_H}:flags=area,format=gray,showinfo",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-",
        ], timeout=_TIMEOUT)
        if proc.returncode != 0:
            raise ExtractionFailed(_error_line(proc.stderr) or f"ffmpeg exited with {proc.returncode}")
        times = _PTS_TIME.findall(proc.stderr)
        pixels = proc.stdout
        n = min(len(times), len(pixels) // _FRAME_BYTES)
        if n == 0:
            raise ExtractionFailed("no frame could be decoded")
        frames: list[tuple[float, int]] = []
        last = None
        for i in range(n):
            try:
                t = float(times[i])
            except ValueError:  # NOPTS
                continue
            h = _dhash(pixels[i * _FRAME_BYTES:(i + 1) * _FRAME_BYTES])
            if h is None or (last is not None and (h ^ last).bit_count() <= _SAME_DISTANCE):
                continue
            frames.append((t, h))
            last = h
        start, end = _span(proc.stderr)
        if end is None:
            end = float(times[n - 1])
        return _HEADER.pack(start, end, len(frames)) + b"".join(_FRAME.pack(t, h) for t, h in frames)

    @staticmethod
    def _decode(feature: bytes) -> _Video:
        start, end, n = _HEADER.unpack_from(feature)
        return _Video(start, end, [_FRAME.unpack_from(feature, _HEADER.size + i * _FRAME.size) for i in range(n)])

    def similarity(self, a: bytes, b: bytes) -> float:
        return self.similarities(self.prepare([b]), a)[0]

    def prepare(self, features: Sequence[bytes]) -> Any:
        videos = [self._decode(f) for f in features]
        # A band value found in many videos belongs to a frame they all
        # share (a channel's intro, a logo, a test card). Such a frame tells
        # videos apart no better than a flat one, and would make every query
        # walk a huge index bucket: it is left out, like a flat frame.
        spread: dict[tuple[int, int], int] = defaultdict(int)
        for video in videos:
            for key in {key for _, h in video.frames for key in _band_keys(h)}:
                spread[key] += 1
        chance = sum(spread.values()) / (_BANDS << _BAND_BITS)
        limit = max(_COMMON_MIN_VIDEOS, _COMMON_FACTOR * chance)
        common = frozenset(key for key, n in spread.items() if n > limit)
        if common:
            videos = [_without_common(video, common) for video in videos]
            logger.debug("video: %d band value(s) in more than %d videos are ignored", len(common), limit)
        index: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for v, video in enumerate(videos):
            for j, (_, h) in enumerate(video.frames):
                for key in _band_keys(h):
                    index[key].append((v, j))
        return videos, index, common

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        videos, index, common = prepared
        q = _without_common(self._decode(query), common)
        # Every frame pair within _MATCH_DISTANCE, found through the band index.
        matches: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for i, (_, h) in enumerate(q.frames):
            seen = set()
            for key in _band_keys(h):
                for vj in index.get(key, ()):
                    if vj in seen:
                        continue
                    seen.add(vj)
                    v, j = vj
                    if (h ^ videos[v].frames[j][1]).bit_count() <= _MATCH_DISTANCE:
                        matches[v].append((i, j))
        sims = [0.0] * len(videos)
        for v, pairs in matches.items():
            sims[v] = _aligned_share(q, videos[v], pairs)
        return sims


def _band_keys(h: int) -> list[tuple[int, int]]:
    return [(band, (h >> (band * _BAND_BITS)) & _BAND_MASK) for band in range(_BANDS)]


def _without_common(video: _Video, common: frozenset[tuple[int, int]]) -> _Video:
    """``video`` without the frames that have two or more common band values."""
    if not common:
        return video
    kept = [f for f in video.frames if sum(key in common for key in _band_keys(f[1])) < 2]
    return video if len(kept) == len(video.frames) else _Video(video.start, video.end, kept)


def _aligned_share(a: _Video, b: _Video, pairs: list[tuple[int, int]]) -> float:
    """The share of the shorter video's frames that agree with the other at the best time offset.

    ``pairs`` are the ``(index in a, index in b)`` of matching frames. The
    offset of a pair is ``time in b - time in a``; the window of width
    ``2 * _OFFSET_TOLERANCE`` holding the most distinct frames of the shorter
    video decides the alignment, and those frames are *matched*.

    An unmatched frame counts against the similarity when, at that offset,
    it falls outside the other video's running time (the other video does
    not have that part), or when the other video has a keyframe at nearly
    the same moment (``_SAME_MOMENT``) and it differs. Otherwise it is left
    out: the other video's keyframes fall elsewhere, and in a fast shot a
    second apart is a different picture.
    """
    if (a.end - a.start, len(a.frames)) > (b.end - b.start, len(b.frames)):
        a, b = b, a
        pairs = [(j, i) for i, j in pairs]
    if not a.frames:
        return 0.0
    # From here, ``a`` is the shorter video.
    offsets = sorted((b.frames[j][0] - a.frames[i][0], i) for i, j in pairs)
    best, best_lo, best_hi = 0, 0, 0
    counts: dict[int, int] = defaultdict(int)
    lo = 0
    for hi, (dt, frame) in enumerate(offsets):
        counts[frame] += 1
        while offsets[lo][0] < dt - 2 * _OFFSET_TOLERANCE:
            gone = offsets[lo][1]
            counts[gone] -= 1
            if not counts[gone]:
                del counts[gone]
            lo += 1
        if len(counts) > best:
            best, best_lo, best_hi = len(counts), lo, hi
    if best < min(_MIN_MATCHES, len(a.frames)):
        return 0.0
    window = offsets[best_lo:best_hi + 1]
    matched = {frame for _, frame in window}
    offset = window[len(window) // 2][0]
    b_times = [t for t, _ in b.frames]
    against = 0
    for i, (t, _) in enumerate(a.frames):
        if i in matched:
            continue
        at = t + offset
        if at < b.start - _SAME_MOMENT or at > b.end + _SAME_MOMENT:
            against += 1
            continue
        k = bisect.bisect_left(b_times, at - _SAME_MOMENT)
        if k < len(b_times) and b_times[k] <= at + _SAME_MOMENT:
            against += 1
    return len(matched) / (len(matched) + against)
