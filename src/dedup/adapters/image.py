"""Image adapter: SHA-256 plus pHash or dHash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from ..config import AppConfig
from ..fingerprints import (
    DHASH_VERSION,
    PHASH_VERSION,
    dhash_from_gray,
    phash_from_gray,
    to_hex64,
)
from ..models import Fingerprint
from .base import ContentAdapter, FingerprintError, MatchInfo, hamming_match


def _color_histogram(image: Image.Image) -> list[float]:
    """A compact 4x4x4 RGB histogram, normalized to sum to 1."""
    small = image.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.uint8).reshape(-1, 3)
    quantized = (pixels // 64).astype(np.int64)
    index = quantized[:, 0] * 16 + quantized[:, 1] * 4 + quantized[:, 2]
    counts = np.bincount(index, minlength=64).astype(np.float64)
    total = counts.sum()
    if total > 0:
        counts /= total
    return [round(float(value), 6) for value in counts]


class ImageAdapter(ContentAdapter):
    name = "image"
    extensions = frozenset(
        {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".ico",
            ".heic",
            ".heif",
            ".avif",
        }
    )

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.algorithm = config.image.algorithm
        self.hash_size = config.image.hash_size
        self.fingerprint_kinds = (self.algorithm,)

    def fingerprint_sig(self) -> str:
        payload = {
            "algorithm": self.algorithm,
            "hash_size": self.hash_size,
            "color_bins": 64,
            "version": PHASH_VERSION if self.algorithm == "phash" else DHASH_VERSION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                original_size = image.size
                if (
                    image.mode in {"RGBA", "LA"}
                    or (image.mode == "P" and "transparency" in image.info)
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    image = Image.alpha_composite(background, rgba)
                color = _color_histogram(image)
                gray = image.convert("L")
                if self.algorithm == "phash":
                    size = self.hash_size * 4
                    gray = gray.resize((size, size), Image.Resampling.LANCZOS)
                    array = np.asarray(gray, dtype=np.uint8)
                    value = phash_from_gray(array, self.hash_size)
                    version = PHASH_VERSION
                else:
                    gray = gray.resize(
                        (self.hash_size + 1, self.hash_size), Image.Resampling.LANCZOS
                    )
                    array = np.asarray(gray, dtype=np.uint8)
                    value = dhash_from_gray(array, self.hash_size)
                    version = DHASH_VERSION
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise FingerprintError(f"cannot decode image: {exc}") from exc

        return {
            self.algorithm: Fingerprint(
                kind=self.algorithm,
                version=version,
                value=to_hex64(value),
                meta={
                    "hash_size": self.hash_size,
                    "width": original_size[0],
                    "height": original_size[1],
                    "color": color,
                },
            )
        }

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != self.algorithm:
            return None
        threshold = (
            self.config.image.phash_threshold
            if kind == "phash"
            else self.config.image.dhash_threshold
        )
        info = hamming_match(left, right, threshold)
        if not info.similar:
            return info
        left_color = left.meta.get("color")
        right_color = right.meta.get("color")
        if (
            isinstance(left_color, list)
            and isinstance(right_color, list)
            and len(left_color) == len(right_color)
            and left_color
        ):
            similarity = sum(
                min(float(a), float(b)) for a, b in zip(left_color, right_color)
            )
            info.detail["color_similarity"] = round(similarity, 4)
            if similarity < self.config.image.color_threshold:
                info.similar = False
                info.detail["reason"] = "color histogram"
        return info
