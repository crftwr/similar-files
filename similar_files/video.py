"""Layer 2 for video: a difference hash of frames sampled across the video (ffmpeg).

Needs ``ffmpeg`` and ``ffprobe`` on ``PATH``, and no Python package: ffmpeg
itself scales each frame down to the 9×8 grey picture a difference hash is
computed from.
"""

from __future__ import annotations

import json
import logging
import struct
import subprocess
from typing import Any, Sequence

from .registry import ExtractionFailed, Extractor, register

logger = logging.getLogger(__name__)

#: One frame, scaled for a 64-bit difference hash: 9 columns, 8 rows, 1 byte each.
_W, _H = 9, 8
_FRAME_BYTES = _W * _H
#: A frame whose brightest and darkest pixels differ by less than this is
#: flat (a fade to black, a title card's background): its hash is noise.
_FLAT_RANGE = 12
#: Durations further apart than this share are different videos, whatever
#: their frames say. Re-encoding and resizing keep the duration.
_DURATION_TOLERANCE = 0.05
#: Per-frame flags in the feature.
_OK, _FLAT, _MISSING = 0, 1, 2
_HEADER = struct.Struct("<dH")
_FRAME = struct.Struct("<QB")
#: Seconds an ffmpeg or ffprobe run may take before the file counts as unreadable.
_TIMEOUT = 120


def _run(args: list[str]) -> bytes:
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ExtractionFailed(f"{args[0]} took longer than {_TIMEOUT}s") from exc
    except OSError as exc:
        raise ExtractionFailed(f"cannot run {args[0]}: {exc}") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ExtractionFailed(msg[-1] if msg else f"{args[0]} exited with {proc.returncode}")
    return proc.stdout


def _duration(path: str) -> float:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=duration,codec_type",
        "-of", "json", path,
    ])
    try:
        info = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ExtractionFailed(f"ffprobe gave no readable answer: {exc}") from exc
    if not info.get("streams"):
        raise ExtractionFailed("no video stream")
    for value in (info.get("format", {}).get("duration"), info["streams"][0].get("duration")):
        try:
            d = float(value)
        except (TypeError, ValueError):
            continue
        if d > 0:
            return d
    raise ExtractionFailed("unknown duration")


def _dhash(pixels: bytes) -> tuple[int, int]:
    """``(hash, flag)`` of one 9×8 grey frame."""
    if len(pixels) < _FRAME_BYTES:
        return 0, _MISSING
    if max(pixels) - min(pixels) < _FLAT_RANGE:
        return 0, _FLAT
    value = 0
    for row in range(_H):
        base = row * _W
        for col in range(_W - 1):
            value = (value << 1) | (pixels[base + col] < pixels[base + col + 1])
    return value, _OK


@register
class VideoExtractor(Extractor):
    """Frames sampled evenly across the video, each reduced to a 64-bit difference hash.

    Two videos are compared frame by frame, at the same share of their
    running time, so a re-encoded, resized or re-muxed copy lines up with
    its original. Each frame pair scores ``1 - 2 * distance / 64``; the
    similarity is the mean, and 0 when the durations differ by more than 5%.
    Flat frames (black, a plain background) are left out of the mean.
    """

    name = "video"
    version = 1
    extensions = frozenset({
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
        ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv",
    })
    requires_programs = ("ffmpeg", "ffprobe")
    needs_path = True
    default_params = {"frames": 10}
    default_threshold = 0.85
    description = "Similar videos: re-encoded, resized or re-muxed copies (frame hashes, ffmpeg)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if not 2 <= params["frames"] <= 100:
            raise ValueError("frames must be 2–100")

    def extract_file(self, path: str) -> bytes:
        duration = _duration(path)
        n = self.params["frames"]
        frames = []
        for i in range(n):
            # Frame centres, never the very start or end: those are black too often.
            t = duration * (i + 0.5) / n
            pixels = _run([
                "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", path,
                "-frames:v", "1", "-vf", f"scale={_W}:{_H}:flags=area,format=gray",
                "-f", "rawvideo", "-",
            ])
            frames.append(_dhash(pixels[:_FRAME_BYTES]))
        if all(flag == _MISSING for _, flag in frames):
            raise ExtractionFailed("no frame could be decoded")
        return _HEADER.pack(duration, n) + b"".join(_FRAME.pack(h, f) for h, f in frames)

    @staticmethod
    def _decode(feature: bytes) -> tuple[float, list[tuple[int, int]]]:
        duration, n = _HEADER.unpack_from(feature)
        return duration, [_FRAME.unpack_from(feature, _HEADER.size + i * _FRAME.size) for i in range(n)]

    def similarity(self, a: bytes, b: bytes) -> float:
        return _compare(self._decode(a), self._decode(b))

    def prepare(self, features: Sequence[bytes]) -> Any:
        return [self._decode(f) for f in features]

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        q = self._decode(query)
        return [_compare(q, p) for p in prepared]


def _compare(a: tuple[float, list], b: tuple[float, list]) -> float:
    (da, fa), (db, fb) = a, b
    longest = max(da, db)
    if longest <= 0 or abs(da - db) / longest > _DURATION_TOLERANCE or len(fa) != len(fb):
        return 0.0
    total, counted = 0.0, 0
    for (ha, ga), (hb, gb) in zip(fa, fb):
        if ga == _MISSING or gb == _MISSING or (ga == _FLAT and gb == _FLAT):
            continue
        counted += 1
        if ga == _OK and gb == _OK:
            total += max(0.0, 1.0 - 2.0 * (ha ^ hb).bit_count() / 64)
        # One flat, one not: that frame pair scores 0.
    return total / counted if counted else 0.0
