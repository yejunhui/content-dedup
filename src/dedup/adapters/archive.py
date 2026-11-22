"""Archive adapter: manifest hash of (relative path, size, inner SHA-256)."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import posixpath
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from ..models import Fingerprint
from ..util import hash_stream
from .base import ContentAdapter, FingerprintError, MatchInfo

ARCHIVE_VERSION = 1


class ArchiveAdapter(ContentAdapter):
    name = "archive"
    extensions = frozenset(
        {
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
            ".gz",
            ".bz2",
            ".xz",
        }
    )
    fingerprint_kinds = ("manifest",)

    def fingerprint_sig(self) -> str:
        payload = {
            "version": ARCHIVE_VERSION,
            "max_entries": self.config.archive.max_entries,
            "max_uncompressed_bytes": self.config.archive.max_uncompressed_bytes,
            "max_inner_hash_bytes": self.config.archive.max_inner_hash_bytes,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        lower = path.name.lower()
        if lower.endswith(".zip"):
            entries, kind = self._zip_manifest(path)
        elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            entries, kind = self._tar_manifest(path)
        elif lower.endswith(".gz"):
            entries, kind = self._single_stream_manifest(path, gzip.open)
        elif lower.endswith(".bz2"):
            entries, kind = self._single_stream_manifest(path, bz2.open)
        elif lower.endswith(".xz"):
            entries, kind = self._single_stream_manifest(path, lzma.open)
        else:
            raise FingerprintError(f"unsupported archive type: {path.suffix}")

        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        manifest_payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
        content_entries = sorted((size, digest) for _, size, digest in entries)
        content_hash = hashlib.sha256(
            json.dumps(content_entries, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        total_bytes = sum(size for _, size, _ in entries)
        return {
            "manifest": Fingerprint(
                kind="manifest",
                version=ARCHIVE_VERSION,
                value=manifest_hash,
                meta={
                    "format": kind,
                    "entries": len(entries),
                    "total_bytes": total_bytes,
                    "content_hash": content_hash,
                },
            )
        }

    def _check_limits(self, count: int, total: int) -> None:
        if count > self.config.archive.max_entries:
            raise FingerprintError(
                f"archive has too many entries ({count} > {self.config.archive.max_entries})"
            )
        if total > self.config.archive.max_uncompressed_bytes:
            raise FingerprintError(
                "archive exceeds max_uncompressed_bytes "
                f"({total} > {self.config.archive.max_uncompressed_bytes})"
            )

    def _hash_member(self, stream: Any, declared_size: int) -> tuple[int, str]:
        if declared_size > self.config.archive.max_inner_hash_bytes:
            raise FingerprintError(
                f"archive member exceeds max_inner_hash_bytes ({declared_size})"
            )
        try:
            digest, actual_size = hash_stream(stream, max_bytes=self.config.archive.max_inner_hash_bytes)
        except ValueError as exc:
            raise FingerprintError(str(exc)) from exc
        return actual_size, digest

    def _zip_manifest(self, path: Path) -> tuple[list[list[Any]], str]:
        entries: list[list[Any]] = []
        total = 0
        try:
            with zipfile.ZipFile(path) as archive:
                infos = sorted(archive.infolist(), key=lambda item: item.filename)
                files = [info for info in infos if not info.is_dir()]
                self._check_limits(len(files), 0)
                for info in files:
                    self._check_limits(len(entries) + 1, total + info.file_size)
                    with archive.open(info, "r") as stream:
                        size, digest = self._hash_member(stream, info.file_size)
                    total += size
                    name = posixpath.normpath(info.filename.replace("\\", "/"))
                    entries.append([name, size, digest])
        except zipfile.BadZipFile as exc:
            raise FingerprintError(f"invalid zip archive: {exc}") from exc
        except RuntimeError as exc:
            raise FingerprintError(f"cannot read zip archive (encrypted?): {exc}") from exc
        self._check_limits(len(entries), total)
        return entries, "zip"

    def _tar_manifest(self, path: Path) -> tuple[list[list[Any]], str]:
        entries: list[list[Any]] = []
        total = 0
        try:
            with tarfile.open(path, mode="r:*") as archive:
                members = sorted(archive.getmembers(), key=lambda item: item.name)
                for member in members:
                    if member.isdir():
                        continue
                    self._check_limits(len(entries) + 1, total)
                    name = posixpath.normpath(member.name.replace("\\", "/"))
                    if member.isfile():
                        if total + member.size > self.config.archive.max_uncompressed_bytes:
                            raise FingerprintError(
                                "archive exceeds max_uncompressed_bytes "
                                f"({total + member.size} > "
                                f"{self.config.archive.max_uncompressed_bytes})"
                            )
                        stream = archive.extractfile(member)
                        if stream is None:
                            continue
                        with stream:
                            size, digest = self._hash_member(stream, member.size)
                        total += size
                        entries.append([name, size, digest])
                    elif member.issym():
                        entries.append([name, -1, f"symlink:{member.linkname}"])
                    elif member.islnk():
                        entries.append([name, -1, f"hardlink:{member.linkname}"])
                    else:
                        entries.append([name, -1, f"special:{member.type!r}"])
        except tarfile.TarError as exc:
            raise FingerprintError(f"invalid tar archive: {exc}") from exc
        self._check_limits(len(entries), total)
        return entries, "tar"

    def _single_stream_manifest(self, path: Path, opener: Any) -> tuple[list[list[Any]], str]:
        try:
            with opener(path, "rb") as stream:
                size, digest = self._hash_member(stream, 0)
        except (OSError, EOFError, lzma.LZMAError) as exc:
            raise FingerprintError(f"cannot decompress archive: {exc}") from exc
        self._check_limits(1, size)
        return [["", size, digest]], "single-stream"

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != "manifest":
            return None
        equal = str(left.value) == str(right.value)
        return MatchInfo(
            similar=equal,
            distance=0 if equal else None,
            score=1.0 if equal else 0.0,
            detail={"layer": "archive-manifest"},
        )
