"""Content adapter base class and match result type."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..config import AppConfig
from ..models import Fingerprint
from ..util import sha256_file


class FingerprintError(RuntimeError):
    """A fingerprint could not be computed, but the file is still valid."""


class UnsupportedFingerprint(FingerprintError):
    """The adapter intentionally does not fingerprint this content."""


@dataclass(slots=True)
class MatchInfo:
    similar: bool
    distance: float | None = None
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class ContentAdapter(ABC):
    name: ClassVar[str] = ""
    extensions: ClassVar[frozenset[str]] = frozenset()
    filenames: ClassVar[frozenset[str]] = frozenset()
    fingerprint_kinds: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @property
    def supports_fingerprint(self) -> bool:
        return bool(self.fingerprint_kinds)

    def exact_hash(self, path: Path) -> str:
        return sha256_file(path)

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        if not self.fingerprint_kinds:
            raise UnsupportedFingerprint(f"{self.name} is exact-hash only")
        raise UnsupportedFingerprint(f"{self.name} does not implement a fingerprint")

    def fingerprint_sig(self) -> str:
        """Signature of all settings that influence fingerprint values."""
        return ""

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        return None


def hamming_match(left: Fingerprint, right: Fingerprint, threshold: int) -> MatchInfo:
    from ..fingerprints import hamming_hex

    distance = hamming_hex(str(left.value), str(right.value))
    return MatchInfo(
        similar=distance <= threshold,
        distance=distance,
        score=1.0 - distance / 64.0,
        detail={"threshold": threshold},
    )
