"""Indexes used to turn expensive pairwise comparisons into candidate lookups."""

from __future__ import annotations

from collections import defaultdict


def split_bits(bits: int, bands: int) -> list[int]:
    """Split a bit width into nearly equal disjoint bands."""
    if bands < 1:
        raise ValueError("bands must be positive")
    if bands > bits:
        raise ValueError("cannot split into more bands than bits")
    base, extra = divmod(bits, bands)
    return [base + 1] * extra + [base] * (bands - extra)


class HammingIndex:
    """Banded index for Hamming-distance candidate generation.

    With threshold ``t``, values are split into ``t + 1`` disjoint bands. If
    two values differ in at most ``t`` bits, at least one band must be exactly
    equal (pigeonhole principle), so the candidate set is a superset of all
    true matches. Callers must verify each candidate with the exact distance.
    """

    def __init__(self, threshold: int, bits: int = 64) -> None:
        if threshold < 0:
            raise ValueError("threshold must be non-negative")
        if bits < 1:
            raise ValueError("bits must be positive")
        if threshold >= bits:
            raise ValueError("threshold must be smaller than the bit width")
        self.threshold = threshold
        self.bits = bits
        self._band_sizes = split_bits(bits, threshold + 1)
        self._maps: list[dict[int, list[int]]] = [defaultdict(list) for _ in self._band_sizes]
        self._count = 0

    def add(self, item_id: int, value: int) -> None:
        self._count += 1
        offset = 0
        masked = int(value) & ((1 << self.bits) - 1)
        for band_index, size in enumerate(self._band_sizes):
            chunk = (masked >> offset) & ((1 << size) - 1)
            self._maps[band_index][chunk].append(item_id)
            offset += size

    def query(self, value: int) -> set[int]:
        offset = 0
        masked = int(value) & ((1 << self.bits) - 1)
        candidates: set[int] = set()
        for band_index, size in enumerate(self._band_sizes):
            chunk = (masked >> offset) & ((1 << size) - 1)
            candidates.update(self._maps[band_index].get(chunk, ()))
            offset += size
        return candidates

    def __len__(self) -> int:
        return self._count
