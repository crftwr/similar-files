"""Layer 2 for images: perceptual hashes (Pillow and imagehash, the ``[image]`` extra)."""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Sequence

from .registry import ExtractionFailed, Extractor, register

logger = logging.getLogger(__name__)

#: Parameter value → imagehash function.
_ALGORITHMS = {"phash": "phash", "dhash": "dhash", "ahash": "average_hash", "whash": "whash"}


@register
class ImageExtractor(Extractor):
    """A perceptual hash of the picture, compared by Hamming distance.

    The similarity is ``1 - 2 * distance / bits``, clamped at 0: identical
    hashes are 1.0, and two unrelated pictures (about half the bits differ)
    are near 0.0.
    """

    name = "image"
    version = 1
    extensions = frozenset(
        {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
    )
    requires = ("PIL", "imagehash")
    extra = "image"
    default_params = {"algorithm": "phash", "hash_size": 8}
    default_threshold = 0.8
    description = "Similar images: resized, re-encoded or lightly edited (perceptual hash)"

    def validate_params(self, params: dict[str, Any]) -> None:
        if params["algorithm"] not in _ALGORITHMS:
            raise ValueError(f"algorithm must be one of {', '.join(_ALGORITHMS)}")
        size = params["hash_size"]
        if size < 4 or size > 32 or (params["algorithm"] == "whash" and size & (size - 1)):
            raise ValueError("hash_size must be 4–32 (a power of two for whash)")

    def extract(self, stream: BinaryIO) -> bytes:
        from PIL import Image, ImageOps, UnidentifiedImageError
        import imagehash

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
        fn = getattr(imagehash, _ALGORITHMS[self.params["algorithm"]])
        bits = fn(img, hash_size=size).hash.flatten()
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
