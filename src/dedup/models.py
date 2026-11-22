"""Data objects shared by the pipeline, grouping, reporting and cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Fingerprint:
    kind: str
    version: int
    value: Any
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "value": self.value,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fingerprint:
        return cls(
            kind=str(data["kind"]),
            version=int(data.get("version", 1)),
            value=data.get("value"),
            meta=dict(data.get("meta") or {}),
        )


@dataclass(slots=True)
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    adapter: str
    exact_hash: str
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    fp_status: str = "none"  # none | ok | unsupported | error
    fp_error: str | None = None
    fp_sig: str = ""
    from_cache: bool = False

    def fingerprint(self, kind: str) -> Fingerprint | None:
        return self.fingerprints.get(kind)


@dataclass(slots=True)
class ScanStats:
    files_seen: int = 0
    files_selected: int = 0
    files_processed: int = 0
    cache_hits: int = 0
    exact_reused: int = 0
    errors: int = 0
    skipped_size: int = 0
    skipped_type: int = 0
    skipped_symlink: int = 0
    skipped_excluded: int = 0
    fingerprints_ok: int = 0
    fingerprints_unsupported: int = 0
    fingerprints_error: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = {
            "files_seen": self.files_seen,
            "files_selected": self.files_selected,
            "files_processed": self.files_processed,
            "cache_hits": self.cache_hits,
            "exact_reused": self.exact_reused,
            "errors": self.errors,
            "skipped_size": self.skipped_size,
            "skipped_type": self.skipped_type,
            "skipped_symlink": self.skipped_symlink,
            "skipped_excluded": self.skipped_excluded,
            "fingerprints_ok": self.fingerprints_ok,
            "fingerprints_unsupported": self.fingerprints_unsupported,
            "fingerprints_error": self.fingerprints_error,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }
        return data


@dataclass(slots=True)
class DuplicateMember:
    path: str
    rel_path: str
    size: int
    mtime_ns: int
    adapter: str
    exact_hash: str
    reason: str
    distance: float | None = None
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rel_path": self.rel_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "adapter": self.adapter,
            "exact_hash": self.exact_hash,
            "reason": self.reason,
            "distance": self.distance,
            "score": self.score,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DuplicateMember:
        return cls(
            path=data["path"],
            rel_path=data.get("rel_path", data["path"]),
            size=int(data["size"]),
            mtime_ns=int(data["mtime_ns"]),
            adapter=data.get("adapter", ""),
            exact_hash=data.get("exact_hash", ""),
            reason=data.get("reason", ""),
            distance=data.get("distance"),
            score=data.get("score"),
            detail=dict(data.get("detail") or {}),
        )


@dataclass(slots=True)
class DuplicateGroup:
    group_id: str
    kind: str
    keeper: DuplicateMember
    duplicates: list[DuplicateMember]
    reclaimable_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.group_id,
            "kind": self.kind,
            "keeper": self.keeper.to_dict(),
            "duplicates": [d.to_dict() for d in self.duplicates],
            "reclaimable_bytes": self.reclaimable_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DuplicateGroup:
        return cls(
            group_id=str(data.get("id", "")),
            kind=data.get("kind", ""),
            keeper=DuplicateMember.from_dict(data["keeper"]),
            duplicates=[DuplicateMember.from_dict(d) for d in data.get("duplicates", [])],
            reclaimable_bytes=int(data.get("reclaimable_bytes", 0)),
        )


@dataclass(slots=True)
class ScanResult:
    root: str
    mode: str
    layers: list[str]
    records: list[FileRecord]
    groups: list[DuplicateGroup]
    stats: ScanStats
    warnings: list[str]
    generated_at: str
