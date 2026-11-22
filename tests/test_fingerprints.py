from __future__ import annotations

import random

import numpy as np
import pytest

from dedup.fingerprints import (
    audio_similarity,
    dhash_from_gray,
    hamming_int,
    phash_from_gray,
    sequence_distance,
    simhash64,
    text_tokens,
    to_hex64,
)
from dedup.index import HammingIndex, split_bits


def test_simhash_near_and_far() -> None:
    base = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 40
    near = base + "extra"
    far = "completely unrelated words about music painting travel " * 40
    base_hash, base_tokens = simhash64(base)
    near_hash, _ = simhash64(near)
    far_hash, _ = simhash64(far)
    assert base_tokens > 100
    assert hamming_int(base_hash, near_hash) <= 6
    assert hamming_int(base_hash, far_hash) >= 20


def test_simhash_empty_and_short() -> None:
    assert simhash64("") == (0, 0)
    value, tokens = simhash64("hello world")
    assert tokens == 2
    assert 0 <= value < (1 << 64)


def test_simhash_tokenizer_splits_cjk() -> None:
    tokens = text_tokens("Hello 世界，测试")
    assert "hello" in tokens
    assert "世" in tokens
    assert "界" in tokens
    assert "测" in tokens


def test_phash_identical_and_noise() -> None:
    rng = np.random.default_rng(7)
    base = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
    assert phash_from_gray(base) == phash_from_gray(base.copy())
    other = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
    assert 10 <= hamming_int(phash_from_gray(base), phash_from_gray(other)) <= 54


def test_dhash_shape() -> None:
    array = np.tile(np.arange(9, dtype=np.uint8), (8, 1))
    value = dhash_from_gray(array, 8)
    assert 0 <= value < (1 << 64)
    with pytest.raises(ValueError):
        dhash_from_gray(np.zeros((8, 8), dtype=np.uint8), 8)


@pytest.mark.parametrize("threshold", [0, 1, 3, 8])
def test_hamming_index_candidates_are_a_superset(threshold: int) -> None:
    rng = random.Random(1234 + threshold)
    values = [rng.getrandbits(64) for _ in range(400)]
    index = HammingIndex(threshold)
    for item_id, value in enumerate(values):
        index.add(item_id, value)
    for value in values[:80]:
        candidates = index.query(value)
        expected = {
            item_id
            for item_id, other in enumerate(values)
            if hamming_int(value, other) <= threshold
        }
        assert expected <= candidates


def test_split_bits_covers_all_bits() -> None:
    for bands in range(1, 20):
        sizes = split_bits(64, bands)
        assert sum(sizes) == 64
        assert len(sizes) == bands
        assert max(sizes) - min(sizes) <= 1


def test_sequence_distance_identical_and_shifted() -> None:
    rng = random.Random(7)
    base = [rng.getrandbits(64) for _ in range(20)]
    assert sequence_distance(base, base) == 0.0
    shifted = [base[0]] + base[:-1]
    shifted_distance = sequence_distance(base, shifted)
    assert shifted_distance is not None
    assert shifted_distance < 12
    other = [rng.getrandbits(64) for _ in range(20)]
    other_distance = sequence_distance(base, other)
    assert other_distance is not None
    assert other_distance > 20


def test_audio_similarity_with_offset_and_noise() -> None:
    rng = random.Random(99)
    base = [rng.getrandbits(32) for _ in range(300)]
    offset = 7
    shifted = [rng.getrandbits(32) for _ in range(offset)] + base[:-offset]
    score, shift, overlap = audio_similarity(base, shifted, max_shift=20, min_overlap=100)
    assert score > 0.95
    assert shift in {offset, -offset}
    assert overlap >= 100
    unrelated = [rng.getrandbits(32) for _ in range(300)]
    other_score, _, _ = audio_similarity(base, unrelated, max_shift=20, min_overlap=100)
    assert other_score < 0.65


def test_to_hex64() -> None:
    assert to_hex64(0xDEADBEEF) == "00000000deadbeef"
