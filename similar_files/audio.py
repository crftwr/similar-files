"""Layer 2 for audio: Chromaprint fingerprints (``fpcalc``).

Needs ``fpcalc`` on ``PATH`` (Chromaprint: ``brew install chromaprint``,
``apt install libchromaprint-tools``, or https://acoustid.org/chromaprint),
and no Python package.
"""

from __future__ import annotations

import json
import logging
import struct
from typing import Any, Sequence

from .registry import ExtractionFailed, Extractor, register, run_program

logger = logging.getLogger(__name__)

_HEADER = struct.Struct("<dI")
#: Fingerprint items two recordings may be shifted by (one item ≈ 0.124 s),
#: for a copy with a little more or less silence at the start.
_MAX_SHIFT = 12
#: The overlap must cover this share of the shorter fingerprint.
_MIN_OVERLAP = 0.5
#: Durations further apart than this (seconds, or share of the longer) are
#: different recordings; cheaper than comparing the fingerprints.
_DURATION_SLACK_S = 3.0
_DURATION_SLACK = 0.1
_TIMEOUT = 120


@register
class AudioExtractor(Extractor):
    """A Chromaprint fingerprint of the first ``length`` seconds, compared by bit error rate.

    The fingerprints are aligned at the best of a few small shifts. The
    similarity is ``1 - 2 * bit_error_rate``, clamped at 0: the same
    recording re-encoded scores near 1, and unrelated audio (about half the
    bits differ) near 0. Durations that differ by more than 3 s and 10%
    score 0 without comparing.
    """

    name = "audio"
    version = 1
    extensions = frozenset({
        ".mp3", ".m4a", ".aac", ".flac", ".wav", ".aif", ".aiff", ".ogg", ".oga",
        ".opus", ".wma", ".alac", ".ape", ".wv",
    })
    requires_programs = ("fpcalc",)
    needs_path = True
    default_params = {"length": 120}
    default_threshold = 0.7
    description = "Similar audio: the same recording re-encoded or re-tagged (Chromaprint)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if not 10 <= params["length"] <= 3600:
            raise ValueError("length must be 10–3600 seconds")

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
    def _decode(feature: bytes) -> tuple[float, int, int]:
        """``(duration, items, fingerprint as one integer)``: item ``i`` in bits ``32i…32i+31``."""
        duration, n = _HEADER.unpack_from(feature)
        return duration, n, int.from_bytes(feature[_HEADER.size:_HEADER.size + 4 * n], "little")

    def similarity(self, a: bytes, b: bytes) -> float:
        return _compare(self._decode(a), self._decode(b))

    def prepare(self, features: Sequence[bytes]) -> Any:
        return [self._decode(f) for f in features]

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        q = self._decode(query)
        return [_compare(q, p) for p in prepared]


def _compare(a: tuple[float, int, int], b: tuple[float, int, int]) -> float:
    (da, na, fa), (db, nb, fb) = a, b
    if abs(da - db) > max(_DURATION_SLACK_S, _DURATION_SLACK * max(da, db)):
        return 0.0
    need = max(1, int(min(na, nb) * _MIN_OVERLAP))
    best = 0.0
    for shift in range(-_MAX_SHIFT, _MAX_SHIFT + 1):
        # Item i of one lines up with item i + shift of the other.
        x, y, nx, ny = (fa, fb, na, nb) if shift >= 0 else (fb, fa, nb, na)
        k = abs(shift)
        overlap = min(nx - k, ny)
        if overlap < need:
            continue
        bits = 32 * overlap
        mask = (1 << bits) - 1
        errors = (((x >> (32 * k)) ^ y) & mask).bit_count()
        best = max(best, 1.0 - 2.0 * errors / bits)
    return best
