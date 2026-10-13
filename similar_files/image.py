"""Layer 2 for images: perceptual hashes, computed with Pillow alone.

pHash, dHash and aHash are written out here rather than taken from
``imagehash``, which needs numpy, SciPy and PyWavelets — about 140 MB beside a
Pillow that a host such as XeFM already ships. Each follows imagehash's own
definition step for step (the same resize, the same comparison), so a hash
means what it meant; the only work Pillow does not do is pHash's DCT, and that
is a few thousand multiplications on a 32×32 picture. ``whash`` (wavelets)
still needs ``imagehash`` and is offered only where it is installed.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import statistics
from typing import Any, BinaryIO, Sequence

from .registry import ExtractionFailed, Extractor, register

logger = logging.getLogger(__name__)

_ALGORITHMS = ("phash", "dhash", "ahash", "whash")


def _gray(img, width: int, height: int) -> list[list[float]]:
    """``img`` as grayscale ``height`` rows of ``width`` values, resized the
    way imagehash resizes (Lanczos)."""
    from PIL import Image

    small = img.convert("L").resize((width, height), Image.Resampling.LANCZOS)
    data = list(small.tobytes())   # one byte per pixel in mode "L"
    return [data[r * width:(r + 1) * width] for r in range(height)]


def _dct_low(values: list[float], keep: int) -> list[float]:
    """The first ``keep`` coefficients of an unnormalised DCT-II — SciPy's
    ``fftpack.dct`` default, which imagehash's pHash uses."""
    n = len(values)
    return [2.0 * sum(x * math.cos(math.pi * k * (2 * i + 1) / (2 * n))
                      for i, x in enumerate(values))
            for k in range(keep)]


def phash_bits(img, hash_size: int) -> list[bool]:
    """imagehash.phash: DCT of a ``4·hash_size`` square, its low-frequency
    ``hash_size`` square compared with that square's median."""
    side = hash_size * 4
    rows = _gray(img, side, side)
    cols = [_dct_low([rows[r][c] for r in range(side)], hash_size) for c in range(side)]
    low = [_dct_low([cols[c][k] for c in range(side)], hash_size) for k in range(hash_size)]
    flat = [v for row in low for v in row]
    med = statistics.median(flat)
    return [v > med for v in flat]


def dhash_bits(img, hash_size: int) -> list[bool]:
    """imagehash.dhash: each pixel against its right-hand neighbour."""
    rows = _gray(img, hash_size + 1, hash_size)
    return [row[c + 1] > row[c] for row in rows for c in range(hash_size)]


def ahash_bits(img, hash_size: int) -> list[bool]:
    """imagehash.average_hash: each pixel against the mean."""
    rows = _gray(img, hash_size, hash_size)
    flat = [v for row in rows for v in row]
    mean = sum(flat) / len(flat)
    return [v > mean for v in flat]


def whash_bits(img, hash_size: int) -> list[bool]:
    import imagehash
    return [bool(b) for b in imagehash.whash(img, hash_size=hash_size).hash.flatten()]


_HASHERS = {"phash": phash_bits, "dhash": dhash_bits, "ahash": ahash_bits,
            "whash": whash_bits}


@register
class ImageExtractor(Extractor):
    """A perceptual hash of the picture, compared by Hamming distance.

    The similarity is ``1 - 2 * distance / bits``, clamped at 0: identical
    hashes are 1.0, and two unrelated pictures (about half the bits differ)
    are near 0.0.
    """

    name = "image"
    # Still 1: the hashes written out here match imagehash's bit for bit
    # (checked over 40 pictures, every algorithm), so cached ones stay valid.
    version = 1
    extensions = frozenset(
        {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
    )
    requires = ("PIL",)
    extra = "image"
    default_params = {"algorithm": "phash", "hash_size": 8}
    default_threshold = 0.8
    description = "Similar images: resized, re-encoded or lightly edited (perceptual hash)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if params["algorithm"] not in _ALGORITHMS:
            raise ValueError(f"algorithm must be one of {', '.join(_ALGORITHMS)}")
        if params["algorithm"] == "whash" and importlib.util.find_spec("imagehash") is None:
            raise ValueError('whash needs imagehash: pip install "similar-files[whash]"')
        size = params["hash_size"]
        if size < 4 or size > 32 or (params["algorithm"] == "whash" and size & (size - 1)):
            raise ValueError("hash_size must be 4–32 (a power of two for whash)")

    def extract(self, stream: BinaryIO) -> bytes:
        from PIL import Image, ImageOps, UnidentifiedImageError

        size = self.params["hash_size"]
        try:
            with Image.open(stream) as img:
                # JPEG decodes at reduced scale much faster; the hash only needs a
                # small picture anyway.
                img.draft("RGB", (size * 16, size * 16))
                img = ImageOps.exif_transpose(img)
                img.load()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ExtractionFailed(str(exc)) from exc
        bits = _HASHERS[self.params["algorithm"]](img, size)
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return value.to_bytes((len(bits) + 7) // 8, "big")

    def similarity(self, a: bytes, b: bytes) -> float:
        bits = len(a) * 8
        distance = (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()
        return max(0.0, 1.0 - 2.0 * distance / bits)

    def prepare(self, features: Sequence[bytes]) -> Any:
        return [int.from_bytes(f, "big") for f in features]

    def similarities(self, prepared: Any, query: bytes) -> Sequence[float]:
        q = int.from_bytes(query, "big")
        scale = 2.0 / (len(query) * 8)
        return [max(0.0, 1.0 - scale * (q ^ v).bit_count()) for v in prepared]
