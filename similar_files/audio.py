"""Layer 2 for audio: Chromaprint fingerprints (``fpcalc``), aligned by offset.

Needs ``fpcalc`` on ``PATH`` (Chromaprint: ``brew install chromaprint``,
``apt install libchromaprint-tools``, or https://acoustid.org/chromaprint),
and no Python package. Comparing two recordings finds the offset at which
their fingerprints agree, so an excerpt, a track inside a longer mix, or a
version with an intro added still matches. The design is in
``doc/dev/AUDIO_SIMILARITY.md``.
"""

from __future__ import annotations

import bisect
import json
import logging
import struct
from array import array
from collections import Counter, defaultdict
from typing import Any, NamedTuple, Sequence

from .registry import ExtractionFailed, Extractor, register, run_program

logger = logging.getLogger(__name__)

_HEADER = struct.Struct("<dI")
#: An item is indexed when the top bits of its scrambled value are zero:
#: 1 in 4. The choice depends on the value alone, so two copies index the
#: same items, and the index is a quarter of the size.
_SAMPLE_BITS = 2
#: A value found more often than this in one fingerprint (silence, a held
#: note) votes for many offsets at once: it is not indexed.
_MAX_REPEATS = 4
#: A value found in more files than this is common to them (a jingle, a
#: station ident) and tells recordings apart no better than silence.
_COMMON_FILES = 50
#: Fewer votes than this for an offset (and its two neighbours) are chance.
_MIN_VOTES = 2
#: The offsets with the most votes that are compared in full, per pair.
_CANDIDATES = 3
#: Seconds an fpcalc run may take before the file counts as unreadable. It
#: decodes the whole file, which for an hour-long mix on a network mount
#: takes a while.
_TIMEOUT = 600


class _Audio(NamedTuple):
    items: int
    #: The fingerprint as one integer: item ``i`` in bits ``32i…32i+31``.
    fingerprint: int


@register
class AudioExtractor(Extractor):
    """A Chromaprint fingerprint, aligned at the best offset and compared by bit error rate.

    The offset is the one most items of the two fingerprints agree on
    exactly. At that offset, each item of the shorter fingerprint scores
    ``1 - 2 * bit_error_rate`` against the item it lines up with, and 0
    where the other recording has no such item; the similarity is the mean,
    clamped at 0. The same recording re-encoded scores near 1, and so does
    an excerpt and the recording it was cut from, or a track and a mix that
    contains it. Unrelated audio (about half the bits differ) scores near 0.

    ``length`` limits the fingerprint to the first seconds of each file;
    0, the default, takes the whole file.
    """

    name = "audio"
    version = 1
    extensions = frozenset({
        ".mp3", ".m4a", ".aac", ".flac", ".wav", ".aif", ".aiff", ".ogg", ".oga",
        ".opus", ".wma", ".alac", ".ape", ".wv",
    })
    requires_programs = ("fpcalc",)
    needs_path = True
    default_params = {"length": 0}
    default_threshold = 0.7
    description = "Similar audio: copies, excerpts, and tracks inside longer mixes (Chromaprint)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if params["length"] != 0 and params["length"] < 10:
            raise ValueError("length must be 0 (the whole file) or at least 10 seconds")

    def extract_file(self, path: str) -> bytes:
        args = ["fpcalc", "-raw", "-json", "-length", str(self.params["length"]), path]
        proc = run_program(args, timeout=_TIMEOUT)
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ExtractionFailed(msg[-1] if msg else f"fpcalc exited with {proc.returncode}")
        try:
            info = json.loads(proc.stdout.decode("utf-8", "replace"))
            duration = float(info["duration"])
            fp = [int(v) & 0xFFFFFFFF for v in info["fingerprint"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise ExtractionFailed(f"fpcalc gave no readable fingerprint: {exc}") from exc
        if not fp:
            raise ExtractionFailed("too short to fingerprint")
        return _HEADER.pack(duration, len(fp)) + struct.pack(f"<{len(fp)}I", *fp)

    @staticmethod
    def _decode(feature: bytes) -> _Audio:
        _, n = _HEADER.unpack_from(feature)
        return _Audio(n, int.from_bytes(feature[_HEADER.size:_HEADER.size + 4 * n], "little"))

    def similarity(self, a: bytes, b: bytes) -> float:
        return self.similarities(self.prepare([b]), a)[0]

    def prepare(self, features: Sequence[bytes]) -> Any:
        audios = [self._decode(f) for f in features]
        # Every indexed item as one word, ``value << 32 | position``, where a
        # position counts items across all fingerprints, one after another.
        # Sorted arrays of words, one per top byte of the value, cost 8 bytes
        # an item; a dict of Python objects cost thirty times that.
        starts = array("Q")
        buckets = [array("Q") for _ in range(256)]
        position = 0
        for feature, audio in zip(features, audios):
            starts.append(position)
            for i, value in _keys(feature):
                buckets[value >> 24].append(value << 32 | (position + i))
            position += audio.items
        if position >= 1 << 32:
            raise ValueError("too many fingerprint items to compare at once")
        common = 0
        for b, bucket in enumerate(buckets):
            words = sorted(bucket)
            kept = array("Q")
            lo = 0
            while lo < len(words):
                value = words[lo] >> 32
                hi = lo + 1
                while hi < len(words) and words[hi] >> 32 == value:
                    hi += 1
                if hi - lo > _COMMON_FILES and len({_file(starts, w) for w in words[lo:hi]}) > _COMMON_FILES:
                    common += 1
                else:
                    kept.extend(words[lo:hi])
                lo = hi
            buckets[b] = kept
        if common:
            logger.debug("audio: %d fingerprint value(s) in more than %d files are ignored", common, _COMMON_FILES)
        return audios, starts, buckets

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        audios, starts, buckets = prepared
        q = self._decode(query)
        # Votes for each offset (item i of the query lines up with item j of the other).
        votes: dict[int, Counter[int]] = defaultdict(Counter)
        for i, value in _keys(query):
            bucket = buckets[value >> 24]
            k = bisect.bisect_left(bucket, value << 32)
            while k < len(bucket) and bucket[k] >> 32 == value:
                v = _file(starts, bucket[k])
                votes[v][(bucket[k] & 0xFFFFFFFF) - starts[v] - i] += 1
                k += 1
        sims = [0.0] * len(audios)
        for v, offsets in votes.items():
            sims[v] = _best(q, audios[v], offsets)
        return sims


def _file(starts: array, word: int) -> int:
    """The index of the fingerprint an index word's position falls in."""
    return bisect.bisect_right(starts, word & 0xFFFFFFFF) - 1


def _keys(feature: bytes) -> list[tuple[int, int]]:
    """``(item index, value)`` of the items of ``feature`` that are indexed."""
    _, n = _HEADER.unpack_from(feature)
    values = struct.unpack_from(f"<{n}I", feature, _HEADER.size)
    sampled = [(i, v) for i, v in enumerate(values) if (v * 0x9E3779B1) & 0xFFFFFFFF < 1 << (32 - _SAMPLE_BITS)]
    repeats = Counter(v for _, v in sampled)
    return [(i, v) for i, v in sampled if repeats[v] <= _MAX_REPEATS]


def _best(a: _Audio, b: _Audio, offsets: Counter[int]) -> float:
    """The similarity of ``a`` and ``b`` at the best of the offsets with the most votes.

    A re-encoded copy can start a fraction of an item earlier or later, so
    its votes split between two neighbouring offsets: they are counted
    together, and both are compared.
    """
    best = 0.0
    for d, _ in offsets.most_common(_CANDIDATES):
        if offsets[d - 1] + offsets[d] + offsets[d + 1] < _MIN_VOTES:
            continue
        for shift in (d - 1, d, d + 1):
            best = max(best, _score(a, b, shift))
    return best


def _score(a: _Audio, b: _Audio, shift: int) -> float:
    """The mean of ``1 - 2 * bit_error_rate`` over the shorter fingerprint, item ``i`` of ``a`` on ``i + shift`` of ``b``."""
    (na, fa), (nb, fb) = a, b
    # Shift the one that starts earlier, so item 0 of each lines up.
    x, y, nx, ny = (fb, fa, nb, na) if shift >= 0 else (fa, fb, na, nb)
    k = abs(shift)
    overlap = min(nx - k, ny)
    if overlap <= 0:
        return 0.0
    errors = (((x >> (32 * k)) ^ y) & ((1 << (32 * overlap)) - 1)).bit_count()
    # Each item scores 1 - 2 * errors / 32; an item outside the overlap scores 0.
    return max(0.0, overlap - errors / 16) / min(na, nb)
