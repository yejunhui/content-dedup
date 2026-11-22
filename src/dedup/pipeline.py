"""Scanning pipeline: enumerate, hash, fingerprint, cache and group."""

from __future__ import annotations

import fnmatch
import logging
import os
import stat as stat_module
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .adapters.base import ContentAdapter, FingerprintError, UnsupportedFingerprint
from .config import AppConfig
from .grouping import Grouper
from .models import FileRecord, ScanResult, ScanStats
from .store import CacheStore
from .util import safe_relpath, utc_now_iso

logger = logging.getLogger("dedup.pipeline")

VALID_MODES = {"exact", "fingerprint", "all"}

ProgressCallback = Callable[[dict[str, Any]], None]


class ScanCancelled(RuntimeError):
    """Raised when a web/UI user cancels a running scan."""


@dataclass(slots=True)
class Candidate:
    path: str
    size: int
    mtime_ns: int
    adapter: ContentAdapter


class Scanner:
    def __init__(
        self,
        config: AppConfig,
        store: CacheStore,
        registry: AdapterRegistry,
        *,
        exclude_paths: list[str | Path] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry
        self.stats = ScanStats()
        self.warnings: list[str] = []
        self._excluded_paths = {
            os.path.abspath(os.fspath(path)) for path in (exclude_paths or [])
        }
        self._expanded_excluded: set[str] = set()
        for path in self._excluded_paths:
            self._expanded_excluded.add(path)
            self._expanded_excluded.add(path + "-wal")
            self._expanded_excluded.add(path + "-shm")
        self._signature_cache: dict[str, str] = {}
        self._progress_callback: ProgressCallback | None = None
        self._cancel_event: threading.Event | None = None
        self._progress_total = 0

    def scan(
        self,
        root: str | Path,
        *,
        mode: str = "all",
        types: set[str] | None = None,
        limit: int | None = None,
        refresh: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ScanResult:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(f"scan root is not a directory: {root_path}")
        if self.config.semantic.enabled:
            raise ValueError("the semantic layer is not implemented in this version")

        if types is not None:
            unknown = types - set(self.registry.names)
            if unknown:
                raise ValueError(
                    f"unknown content types: {', '.join(sorted(unknown))}; "
                    f"available: {', '.join(self.registry.names)}"
                )

        self.stats = ScanStats()
        self.warnings = []
        self._progress_callback = progress_callback
        self._cancel_event = cancel_event
        self._progress_total = 0
        started = time.monotonic()
        self._emit("enumerating", 0, 0, "正在遍历文件…")

        candidates = list(self._enumerate(root_path, types, limit))
        self._check_cancel()
        self.stats.files_selected = len(candidates)
        self._progress_total = len(candidates)
        self._emit("hashing", 0, len(candidates), f"准备处理 {len(candidates)} 个文件")
        records: list[FileRecord | None] = [None] * len(candidates)
        compute: list[tuple[int, str | None]] = []

        for index, candidate in enumerate(candidates):
            self._check_cancel()
            cached = None
            if not refresh:
                adapter = candidate.adapter
                cached = self.store.get(
                    candidate.path,
                    candidate.size,
                    candidate.mtime_ns,
                    adapter.name,
                    require_fingerprint=mode != "exact",
                    expected_kinds=adapter.fingerprint_kinds,
                    fp_sig=self._fingerprint_sig(adapter),
                )
            if cached is not None:
                records[index] = cached
                self.stats.cache_hits += 1
                self.stats.files_processed += 1
                self._emit(
                    "hashing",
                    self.stats.files_processed,
                    len(candidates),
                    f"复用缓存：{candidate.path}",
                    current=candidate.path,
                )
                continue
            base_hash: str | None = None
            if not refresh and mode != "exact":
                base_hash = self.store.get_exact_hash(
                    candidate.path,
                    candidate.size,
                    candidate.mtime_ns,
                    candidate.adapter.name,
                )
                if base_hash:
                    self.stats.exact_reused += 1
            compute.append((index, base_hash))

        if self.config.scan.workers > 1 and len(compute) > 1:
            with ThreadPoolExecutor(max_workers=self.config.scan.workers) as executor:
                for (index, _), result in zip(
                    compute,
                    executor.map(
                        lambda item: self._compute_one(candidates[item[0]], mode, item[1]),
                        compute,
                    ),
                ):
                    self._check_cancel()
                    self._accept(records, index, result, candidates[index])
        else:
            for index, base_hash in compute:
                self._check_cancel()
                result = self._compute_one(candidates[index], mode, base_hash)
                self._accept(records, index, result, candidates[index])

        self.store.commit()
        self._check_cancel()
        self._emit("grouping", len(candidates), len(candidates), "正在分析重复与相似关系…")
        final_records = [record for record in records if record is not None]
        valid_records = [record for record in final_records if record.exact_hash]
        grouper = Grouper(self.config, self.registry)
        groups = grouper.group(valid_records, str(root_path))
        self.warnings.extend(grouper.warnings)

        self.stats.fingerprints_ok = sum(1 for record in valid_records if record.fp_status == "ok")
        self.stats.fingerprints_unsupported = sum(
            1 for record in valid_records if record.fp_status == "unsupported"
        )
        self.stats.fingerprints_error = sum(
            1 for record in valid_records if record.fp_status == "error"
        )
        unreadable = sum(1 for record in final_records if not record.exact_hash)
        self.stats.errors = max(self.stats.errors, self.stats.fingerprints_error + unreadable)
        self.stats.elapsed_seconds = time.monotonic() - started
        if self.config.scan.progress_every:
            logger.info(
                "scan finished: %d files, %d groups, %d cache hits, %d errors in %.1fs",
                self.stats.files_processed,
                len(groups),
                self.stats.cache_hits,
                self.stats.errors,
                self.stats.elapsed_seconds,
            )
        self._emit(
            "done",
            len(candidates),
            len(candidates),
            f"完成：{len(groups)} 组重复，可回收 {sum(group.reclaimable_bytes for group in groups)} 字节",
            groups=len(groups),
        )

        return ScanResult(
            root=str(root_path),
            mode=mode,
            layers=["exact"] if mode == "exact" else ["exact", "fingerprint"],
            records=final_records,
            groups=groups,
            stats=self.stats,
            warnings=self.warnings,
            generated_at=utc_now_iso(),
        )

    def _accept(
        self,
        records: list[FileRecord | None],
        index: int,
        result: tuple[FileRecord, bool],
        candidate: Candidate,
    ) -> None:
        record, cacheable = result
        records[index] = record
        self.stats.files_processed += 1
        if record.fp_status == "error":
            self.stats.errors += 1
            self.warnings.append(f"{record.path}: {record.fp_error or 'fingerprint error'}")
        if cacheable and record.exact_hash:
            self.store.put(record, commit=(self.stats.files_processed % 100 == 0))
        if self.config.scan.progress_every and (
            self.stats.files_processed % self.config.scan.progress_every == 0
        ):
            logger.info(
                "processed %d/%d (cache hits %d, errors %d)",
                self.stats.files_processed,
                self.stats.files_selected,
                self.stats.cache_hits,
                self.stats.errors,
            )
        self._emit(
            "hashing",
            self.stats.files_processed,
            self.stats.files_selected,
            record.fp_error or f"已处理：{record.path}",
            current=record.path,
        )

    def _emit(
        self,
        phase: str,
        processed: int,
        total: int,
        message: str,
        current: str | None = None,
        **extra: Any,
    ) -> None:
        callback = self._progress_callback
        if callback is None:
            return
        if total > 0:
            percent = round(min(100.0, max(0.0, processed / total * 100.0)), 1)
        else:
            percent = 100.0 if phase == "done" else 0.0
        payload: dict[str, Any] = {
            "phase": phase,
            "processed": int(processed),
            "total": int(total),
            "percent": percent,
            "message": message,
            "current": current,
        }
        payload.update(extra)
        try:
            callback(payload)
        except Exception:
            logger.debug("progress callback failed", exc_info=True)

    def _check_cancel(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ScanCancelled("扫描已取消")

    def _compute_one(
        self,
        candidate: Candidate,
        mode: str,
        base_hash: str | None = None,
    ) -> tuple[FileRecord, bool]:
        adapter = candidate.adapter
        signature = self._fingerprint_sig(adapter)
        record = FileRecord(
            path=candidate.path,
            size=candidate.size,
            mtime_ns=candidate.mtime_ns,
            adapter=adapter.name,
            exact_hash="",
            fp_sig=signature,
        )
        if base_hash is not None:
            record.exact_hash = base_hash
        else:
            try:
                record.exact_hash = adapter.exact_hash(Path(candidate.path))
            except Exception as exc:  # noqa: BLE001 - keep scanning other files
                record.fp_status = "error"
                record.fp_error = f"cannot read file: {exc}"
                return record, False

        if mode != "exact" and adapter.supports_fingerprint:
            try:
                fingerprints = adapter.compute_fingerprint(Path(candidate.path))
                missing = [kind for kind in adapter.fingerprint_kinds if kind not in fingerprints]
                if missing:
                    raise FingerprintError(
                        f"adapter did not return fingerprints: {', '.join(missing)}"
                    )
                record.fingerprints = fingerprints
                record.fp_status = "ok"
            except UnsupportedFingerprint as exc:
                record.fp_status = "unsupported"
                record.fp_error = str(exc)
            except FingerprintError as exc:
                record.fp_status = "error"
                record.fp_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - fingerprints must never abort a scan
                record.fp_status = "error"
                record.fp_error = f"{type(exc).__name__}: {exc}"

        try:
            after = os.stat(candidate.path)
        except OSError as exc:
            record.fingerprints = {}
            record.fp_status = "error"
            record.fp_error = f"stat failed after read: {exc}"
            return record, False
        if after.st_size != candidate.size or after.st_mtime_ns != candidate.mtime_ns:
            record.fingerprints = {}
            record.fp_status = "error"
            record.fp_error = "file changed during scan"
            return record, False

        if mode == "exact":
            record.fp_status = "none"
        elif not adapter.supports_fingerprint:
            record.fp_status = "unsupported"
        return record, True

    def _fingerprint_sig(self, adapter: ContentAdapter) -> str:
        cached = self._signature_cache.get(adapter.name)
        if cached is None:
            cached = adapter.fingerprint_sig()
            self._signature_cache[adapter.name] = cached
        return cached

    def _enumerate(
        self,
        root: Path,
        types: set[str] | None,
        limit: int | None,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        if limit is not None and limit <= 0:
            return candidates
        min_size = self.config.scan.min_size
        follow = self.config.scan.follow_symlinks
        visited_dirs: set[str] = {os.path.realpath(root)} if follow else set()
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=follow, onerror=self._on_walk_error
        ):
            self._check_cancel()
            current = Path(dirpath)
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                full = current / dirname
                rel = safe_relpath(full, root)
                if self._is_excluded(rel, dirname, is_dir=True):
                    continue
                if self._is_excluded_path(full):
                    continue
                if not follow and full.is_symlink():
                    continue
                if follow:
                    real = os.path.realpath(full)
                    if real in visited_dirs:
                        continue
                    visited_dirs.add(real)
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in sorted(filenames):
                self._check_cancel()
                full = current / filename
                self.stats.files_seen += 1
                rel = safe_relpath(full, root)
                if self._is_excluded(rel, filename, is_dir=False) or self._is_excluded_path(full):
                    self.stats.skipped_excluded += 1
                    continue
                if not follow and full.is_symlink():
                    self.stats.skipped_symlink += 1
                    continue
                try:
                    info = full.stat()
                except OSError as exc:
                    self.stats.errors += 1
                    self.warnings.append(f"{full}: cannot stat: {exc}")
                    continue
                if not stat_module.S_ISREG(info.st_mode):
                    continue
                adapter = self.registry.resolve(full)
                if types is not None and adapter.name not in types:
                    self.stats.skipped_type += 1
                    continue
                if info.st_size < min_size:
                    self.stats.skipped_size += 1
                    continue
                candidates.append(
                    Candidate(
                        path=str(full),
                        size=info.st_size,
                        mtime_ns=info.st_mtime_ns,
                        adapter=adapter,
                    )
                )
                if limit is not None and len(candidates) >= limit:
                    return candidates

        return candidates

    def _is_excluded(self, rel_path: str, name: str, *, is_dir: bool) -> bool:
        rel_posix = rel_path.replace(os.sep, "/")
        for pattern in self.config.scan.exclude:
            pattern = pattern.rstrip("/")
            if not pattern:
                continue
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_posix, pattern):
                return True
        return False

    def _is_excluded_path(self, path: Path) -> bool:
        return os.path.abspath(os.fspath(path)) in self._expanded_excluded

    def _on_walk_error(self, error: OSError) -> None:
        self.stats.errors += 1
        self.warnings.append(f"cannot traverse {error.filename or 'directory'}: {error}")
