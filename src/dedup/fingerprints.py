"""Fingerprint algorithms: pHash, dHash, SimHash and similarity helpers."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from functools import lru_cache

import numpy as np

SIMHASH_VERSION = 2
PHASH_VERSION = 1
DHASH_VERSION = 1

CJK_RANGES = (
    "\u3040-\u30ff"  # kana
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
)
_CJK_RE = re.compile(f"[{CJK_RANGES}]")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def popcount(value: int) -> int:
    return int(value).bit_count()


def hamming_int(a: int, b: int) -> int:
    return (int(a) ^ int(b)).bit_count()


def hamming_hex(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


@lru_cache(maxsize=8)
def _dct_matrix(size: int) -> np.ndarray:
    k = np.arange(size, dtype=np.float64).reshape(-1, 1)
    n = np.arange(size, dtype=np.float64).reshape(1, -1)
    matrix = np.cos(np.pi * (2.0 * n + 1.0) * k / (2.0 * size))
    matrix *= np.sqrt(2.0 / size)
    matrix[0, :] *= 1.0 / np.sqrt(2.0)
    return matrix


def phash_from_gray(gray: np.ndarray, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    """Compute a 64-bit pHash from a square grayscale array.

    The caller is responsible for resizing the image to
    ``hash_size * highfreq_factor`` square pixels.
    """
    size = hash_size * highfreq_factor
    if gray.ndim != 2 or gray.shape != (size, size):
        raise ValueError(f"expected grayscale array of shape {(size, size)}, got {gray.shape}")
    matrix = _dct_matrix(size)
    coefficients = matrix @ gray.astype(np.float64) @ matrix.T
    low = coefficients[:hash_size, :hash_size]
    median = float(np.median(low))
    bits = (low > median).reshape(-1)
    packed = np.packbits(bits)
    return int.from_bytes(packed.tobytes()[:8], "big")


def dhash_from_gray(gray: np.ndarray, hash_size: int = 8) -> int:
    """Compute a 64-bit dHash from a (hash_size, hash_size + 1) grayscale array."""
    expected = (hash_size, hash_size + 1)
    if gray.ndim != 2 or gray.shape != expected:
        raise ValueError(f"expected grayscale array of shape {expected}, got {gray.shape}")
    bits = (gray[:, 1:] > gray[:, :-1]).reshape(-1)
    packed = np.packbits(bits)
    return int.from_bytes(packed.tobytes()[:8], "big")


def to_hex64(value: int) -> str:
    return f"{int(value) & 0xFFFFFFFFFFFFFFFF:016x}"


def text_tokens(text: str) -> list[str]:
    """Tokenize text for SimHash.

    Latin/other words stay whole; CJK characters are split individually so
    that whitespace-free Chinese/Japanese/Korean text still produces useful
    shingles.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for word in _WORD_RE.findall(normalized):
        if not _CJK_RE.search(word):
            tokens.append(word)
            continue
        buffer: list[str] = []
        for char in word:
            if _CJK_RE.match(char):
                if buffer:
                    tokens.append("".join(buffer))
                    buffer = []
                tokens.append(char)
            else:
                buffer.append(char)
        if buffer:
            tokens.append("".join(buffer))
    return tokens


def _hash64_feature(feature: str) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8, person=b"simhash").digest()
    return int.from_bytes(digest, "big")


def simhash64_from_tokens(
    tokens: Sequence[str],
    *,
    shingle_size: int = 3,
    max_tokens: int = 200_000,
) -> tuple[int, int]:
    """Return (64-bit SimHash, original token count).

    Unigrams contribute weight 1 and token shingles contribute weight 2, which
    keeps word frequencies while adding word-order sensitivity.
    """
    token_count = len(tokens)
    if token_count == 0:
        return 0, 0

    stride = max(1, math.ceil(token_count / max_tokens))
    sampled = list(tokens[::stride]) if stride > 1 else list(tokens)

    weights: dict[str, int] = {}
    for token in sampled:
        weights[token] = weights.get(token, 0) + 1
    if shingle_size > 0 and len(sampled) >= shingle_size:
        for index in range(len(sampled) - shingle_size + 1):
            feature = "\x1f".join(sampled[index : index + shingle_size])
            weights[feature] = weights.get(feature, 0) + 2

    shifts = np.arange(64, dtype=np.uint64)
    accumulator = np.zeros(64, dtype=np.int64)
    items = list(weights.items())
    chunk_size = 32_768
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        hashes = np.fromiter(
            (_hash64_feature(feature) for feature, _ in chunk),
            dtype=np.uint64,
            count=len(chunk),
        )
        chunk_weights = np.fromiter(
            (weight for _, weight in chunk),
            dtype=np.int64,
            count=len(chunk),
        )
        bits = ((hashes[:, None] >> shifts) & np.uint64(1)).astype(np.int64)
        signed = bits * 2 - 1
        accumulator += (signed * chunk_weights[:, None]).sum(axis=0)

    value = 0
    for bit_index, score in enumerate(accumulator):
        if score > 0:
            value |= 1 << bit_index
    return value, token_count


def simhash64(text: str, *, shingle_size: int = 3, max_tokens: int = 200_000) -> tuple[int, int]:
    return simhash64_from_tokens(text_tokens(text), shingle_size=shingle_size, max_tokens=max_tokens)


def _downsample(sequence: Sequence[int], target: int) -> list[int]:
    if len(sequence) <= target:
        return list(sequence)
    if target <= 1:
        return [sequence[0]]
    indices = np.linspace(0, len(sequence) - 1, target, dtype=np.int64)
    return [sequence[int(index)] for index in indices]


def sequence_distance(
    left: Sequence[int],
    right: Sequence[int],
    *,
    max_cells: int = 2_000_000,
) -> float | None:
    """Normalized DTW distance between two pHash sequences.

    Values are Hamming distances per aligned frame (0 is identical). The
    result is normalized by the longer sequence length, which is a reasonable
    approximation when frame counts differ slightly.
    """
    if not left or not right:
        return None
    a = list(left)
    b = list(right)
    if len(a) * len(b) > max_cells:
        target = max(2, int(math.sqrt(max_cells)))
        if len(a) > target:
            a = _downsample(a, target)
        if len(b) > target:
            b = _downsample(b, target)

    n, m = len(a), len(b)
    band = max(8, abs(n - m) + 4, int(max(n, m) * 0.1))
    infinity = 1 << 60
    previous = [infinity] * (m + 1)
    previous[0] = 0
    for i in range(1, n + 1):
        current = [infinity] * (m + 1)
        center = max(1, min(m, round(i * m / n)))
        low = max(1, center - band)
        high = min(m, center + band)
        for j in range(low, high + 1):
            cost = popcount(a[i - 1] ^ b[j - 1])
            best = previous[j - 1]
            best = min(best, previous[j])
            best = min(best, current[j - 1])
            current[j] = cost + best
        previous = current
    if previous[m] >= infinity:
        return None
    return previous[m] / max(n, m)


_POPCOUNT16 = np.array([value.bit_count() for value in range(1 << 16)], dtype=np.uint8)


def _popcount_u32(array: np.ndarray) -> int:
    values = np.asarray(array, dtype=np.uint32)
    low = _POPCOUNT16[values & np.uint32(0xFFFF)]
    high = _POPCOUNT16[values >> np.uint32(16)]
    return int(low.sum()) + int(high.sum())


def _as_u32(values: Sequence[int]) -> np.ndarray:
    if not values:
        return np.array([], dtype=np.uint32)
    raw = np.asarray(values, dtype=np.int64) & 0xFFFFFFFF
    return raw.astype(np.uint32)


def audio_similarity(
    left: Sequence[int],
    right: Sequence[int],
    *,
    max_shift: int = 30,
    min_overlap: int = 40,
) -> tuple[float, int, int]:
    """Compare Chromaprint raw fingerprints over small time shifts.

    Returns (score in [0, 1], best shift, overlap in frames). Shift > 0 means
    the left fingerprint starts later than the right one.
    """
    a = _as_u32(left)
    b = _as_u32(right)
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0, 0, 0
    required = min(min_overlap, la, lb)
    best_score = 0.0
    best_shift = 0
    best_overlap = 0
    for shift in range(-max_shift, max_shift + 1):
        start_a = max(0, shift)
        start_b = max(0, -shift)
        overlap = min(la - start_a, lb - start_b)
        if overlap < required:
            continue
        difference = a[start_a : start_a + overlap] ^ b[start_b : start_b + overlap]
        errors = _popcount_u32(difference)
        score = 1.0 - errors / (32.0 * overlap)
        if score > best_score:
            best_score = score
            best_shift = shift
            best_overlap = overlap
    return max(0.0, best_score), best_shift, best_overlap
