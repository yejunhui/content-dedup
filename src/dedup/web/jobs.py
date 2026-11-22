"""Background jobs, report storage and directory browsing for the web UI."""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters import AdapterRegistry
from ..cleanup import apply_selections
from ..config import AppConfig
from ..pipeline import VALID_MODES, ScanCancelled, Scanner
from ..report import load_report, write_report
from ..store import CacheStore
from ..util import human_size, timestamp_slug, utc_now_iso

logger = logging.getLogger("dedup.web.jobs")

ACTIVE_STATUSES = {"queued", "running"}


class JobError(RuntimeError):
    pass


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    status: str = "queued"
    progress: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    report_id: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": dict(self.progress),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "report_id": self.report_id,
        }


class JobManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        db_path: str | Path,
        reports_dir: str | Path,
        thumb_dir: str | Path,
        allow_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.db_path = str(db_path)
        self.reports_dir = Path(reports_dir).expanduser().resolve()
        self.thumb_dir = Path(thumb_dir).expanduser().resolve()
        self.allow_root = Path(allow_root).expanduser().resolve() if allow_root else None
        if self.allow_root is not None and not self.allow_root.is_dir():
            raise ValueError(f"web allow root is not a directory: {self.allow_root}")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._active_job: str | None = None
        self._lock = threading.RLock()
        self._report_roots: dict[str, Path] = {}

    # ------------------------------------------------------------------ jobs
    def start_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_idle()
            root = self.validate_scan_root(str(params.get("root") or ""))
            mode = str(params.get("mode") or "all")
            if mode not in VALID_MODES:
                raise ValueError(f"invalid mode: {mode}")
            types = self._parse_types(params.get("types"))
            limit = self._parse_optional_int(params.get("limit"), "limit", minimum=0)
            workers = self._parse_optional_int(params.get("workers"), "workers", minimum=1)
            min_size = self._parse_optional_int(params.get("min_size"), "min_size", minimum=0)
            if types is not None:
                if not types:
                    raise ValueError("请至少选择一种内容类型")
                known = set(AdapterRegistry(self.config).names)
                unknown = types - known
                if unknown:
                    raise ValueError(
                        f"未知内容类型：{', '.join(sorted(unknown))}；"
                        f"可选：{', '.join(sorted(known))}"
                    )
            if workers is not None and workers > 32:
                raise ValueError("workers must be <= 32")
            job_id = uuid.uuid4().hex[:12]
            report_id = f"web-{timestamp_slug()}-{job_id}.json"
            job = Job(
                id=job_id,
                kind="scan",
                status="running",
                started_at=utc_now_iso(),
                report_id=report_id,
                progress={
                    "phase": "starting",
                    "processed": 0,
                    "total": 0,
                    "percent": 0,
                    "message": "正在启动扫描…",
                    "current": None,
                },
            )
            normalized = {
                "root": str(root),
                "mode": mode,
                "types": sorted(types) if types is not None else None,
                "limit": limit,
                "workers": workers,
                "min_size": min_size,
                "follow_symlinks": bool(params.get("follow_symlinks", False)),
                "refresh": bool(params.get("refresh", False)),
            }
            job.thread = threading.Thread(
                target=self._run_scan,
                args=(job, normalized),
                name=f"dedup-scan-{job_id}",
                daemon=True,
            )
            self._jobs[job_id] = job
            self._active_job = job_id
            job.thread.start()
            return job.snapshot()

    def start_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_idle()
            report_id = str(params.get("report_id") or "")
            report = self.get_report(report_id)
            selections = params.get("selections")
            if not isinstance(selections, list):
                raise TypeError("selections must be a list")
            dry_run = bool(params.get("dry_run", False))
            verify = bool(params.get("verify", True))
            trash = params.get("trash")
            if trash is not None and not isinstance(trash, str):
                raise TypeError("trash must be a string or null")
            job_id = uuid.uuid4().hex[:12]
            job = Job(
                id=job_id,
                kind="apply",
                status="running",
                started_at=utc_now_iso(),
                report_id=report_id,
                progress={
                    "phase": "starting",
                    "processed": 0,
                    "total": 0,
                    "percent": 0,
                    "message": "正在准备移动到回收站…",
                    "current": None,
                },
            )
            job.thread = threading.Thread(
                target=self._run_apply,
                args=(job, report, selections, dry_run, verify, trash),
                name=f"dedup-apply-{job_id}",
                daemon=True,
            )
            self._jobs[job_id] = job
            self._active_job = job_id
            job.thread.start()
            return job.snapshot()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            return job.snapshot()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            if job.status in ACTIVE_STATUSES:
                job.cancel_event.set()
                job.progress = {
                    **job.progress,
                    "message": "正在取消…",
                }
            return job.snapshot()

    def _ensure_idle(self) -> None:
        if self._active_job is not None:
            job = self._jobs.get(self._active_job)
            if job is not None and job.status in ACTIVE_STATUSES:
                raise JobError("已有任务在运行，请等待完成或先取消")

    def _finish(self, job: Job) -> None:
        with self._lock:
            job.finished_at = utc_now_iso()
            if self._active_job == job.id:
                self._active_job = None

    def _update_progress(self, job: Job, payload: dict[str, Any]) -> None:
        with self._lock:
            if job.status in ACTIVE_STATUSES:
                job.progress = dict(payload)

    def _run_scan(self, job: Job, params: dict[str, Any]) -> None:
        try:
            config = copy.deepcopy(self.config)
            config.semantic.enabled = False
            if params["workers"] is not None:
                config.scan.workers = params["workers"]
            if params["min_size"] is not None:
                config.scan.min_size = params["min_size"]
            config.scan.follow_symlinks = params["follow_symlinks"]
            config.validate()
            root = self.validate_scan_root(params["root"])
            types = set(params["types"]) if params["types"] is not None else None
            report_path = self.reports_dir / str(job.report_id)
            trash_config = Path(config.safety.trash_dir).expanduser()
            trash_root = trash_config if trash_config.is_absolute() else root / trash_config
            exclude_paths: list[str | Path] = [
                self.db_path,
                self.reports_dir,
                self.thumb_dir,
                report_path,
                report_path.with_suffix(".md"),
                trash_root,
            ]
            with CacheStore(self.db_path) as store:
                scanner = Scanner(
                    config,
                    store,
                    AdapterRegistry(config),
                    exclude_paths=exclude_paths,
                )
                result = scanner.scan(
                    root,
                    mode=params["mode"],
                    types=types,
                    limit=params["limit"],
                    refresh=params["refresh"],
                    progress_callback=lambda payload: self._update_progress(job, payload),
                    cancel_event=job.cancel_event,
                )
            json_path, md_path = write_report(result, config, report_path)
            report = load_report(json_path)
            self._register_report(str(job.report_id), report)
            with self._lock:
                job.result = {
                    "report_id": job.report_id,
                    "summary": report.get("summary") or {},
                    "stats": report.get("stats") or {},
                    "root": report.get("root"),
                    "markdown": str(md_path),
                    "warnings": (report.get("scan_warnings") or {}).get("total", 0),
                    "fingerprint_issues": len(report.get("fingerprint_issues") or []),
                }
                job.status = "done"
                job.progress = {
                    "phase": "done",
                    "processed": job.progress.get("processed", 0),
                    "total": job.progress.get("total", 0),
                    "percent": 100.0,
                    "message": "扫描完成",
                    "current": None,
                }
        except ScanCancelled:
            with self._lock:
                job.status = "cancelled"
                job.error = "扫描已取消"
                job.report_id = None
                job.progress = {**job.progress, "phase": "cancelled", "message": "扫描已取消"}
        except Exception as exc:
            logger.exception("scan job failed")
            with self._lock:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.report_id = None
                job.progress = {**job.progress, "phase": "error", "message": job.error}
        finally:
            self._finish(job)

    def _run_apply(
        self,
        job: Job,
        report: dict[str, Any],
        selections: list[dict[str, Any]],
        dry_run: bool,
        verify: bool,
        trash: str | None,
    ) -> None:
        try:
            summary = apply_selections(
                report,
                selections,
                trash_dir=trash,
                dry_run=dry_run,
                verify=verify,
                progress_callback=lambda payload: self._update_progress(job, payload),
                cancel_event=job.cancel_event,
            )
            with self._lock:
                job.result = summary
                job.status = "cancelled" if summary.get("cancelled") else "done"
                job.progress = {
                    "phase": job.status,
                    "processed": job.progress.get("processed", 0),
                    "total": job.progress.get("total", 0),
                    "percent": 100.0 if job.status == "done" else job.progress.get("percent", 0),
                    "message": "清理完成" if job.status == "done" else "操作已取消",
                    "current": None,
                }
        except Exception as exc:
            logger.exception("apply job failed")
            with self._lock:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.progress = {**job.progress, "phase": "error", "message": job.error}
        finally:
            self._finish(job)

    # --------------------------------------------------------------- reports
    def list_reports(self, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            paths = sorted(
                self.reports_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            paths = []
        for path in paths[: max(0, limit)]:
            try:
                report = load_report(path)
                mtime_ns = path.stat().st_mtime_ns
            except Exception as exc:  # noqa: BLE001 - skip damaged historical reports
                logger.debug("skipping damaged report %s: %s", path, exc)
                continue
            root = report.get("root")
            if not root:
                continue
            resolved_root = Path(str(root)).expanduser().resolve()
            if self.allow_root is not None and not _is_within(resolved_root, self.allow_root):
                continue
            summary = report.get("summary") or {}
            items.append(
                {
                    "id": path.name,
                    "name": path.name,
                    "generated_at": report.get("generated_at"),
                    "root": str(resolved_root),
                    "mode": report.get("mode"),
                    "groups": int(summary.get("groups") or 0),
                    "duplicate_files": int(summary.get("duplicate_files") or 0),
                    "reclaimable_bytes": int(summary.get("reclaimable_bytes") or 0),
                    "reclaimable_human": human_size(int(summary.get("reclaimable_bytes") or 0)),
                    "mtime_ns": mtime_ns,
                }
            )
        return items

    def get_report(self, report_id: str) -> dict[str, Any]:
        safe_name = Path(report_id).name
        if not report_id or safe_name != report_id or not safe_name.endswith(".json"):
            raise ValueError("invalid report id")
        path = (self.reports_dir / safe_name).resolve()
        if path.parent != self.reports_dir or not path.is_file():
            raise FileNotFoundError(f"report not found: {safe_name}")
        report = load_report(path)
        root = report.get("root")
        if not root:
            raise ValueError("report is missing root")
        resolved_root = Path(str(root)).expanduser().resolve()
        if self.allow_root is not None and not _is_within(resolved_root, self.allow_root):
            raise PermissionError("report root is outside the allowed web root")
        with self._lock:
            self._report_roots[safe_name] = resolved_root
        return report

    def _register_report(self, report_id: str, report: dict[str, Any]) -> None:
        root = report.get("root")
        if root:
            with self._lock:
                self._report_roots[report_id] = Path(str(root)).expanduser().resolve()

    def report_roots(self) -> list[Path]:
        with self._lock:
            return list(self._report_roots.values())

    # ------------------------------------------------------------- filesystem
    def validate_scan_root(self, value: str) -> Path:
        if not value or not value.strip():
            raise ValueError("请选择要扫描的目录")
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"目录不存在：{path}")
        if self.allow_root is not None and not _is_within(path, self.allow_root):
            raise PermissionError(f"目录不在允许范围内：{self.allow_root}")
        return path

    def browse(self, value: str | None, *, show_hidden: bool = False) -> dict[str, Any]:
        start = self.allow_root or Path.home()
        if not start.is_dir():
            start = Path.cwd()
        path = Path(value).expanduser().resolve() if value else start.resolve()
        if self.allow_root is not None and not _is_within(path, self.allow_root):
            raise PermissionError(f"目录不在允许范围内：{self.allow_root}")
        if not path.is_dir():
            raise FileNotFoundError(f"目录不存在：{path}")

        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            children = list(os.scandir(path))
        except OSError as exc:
            raise PermissionError(f"无法读取目录：{exc}") from exc
        for entry in children:
            if len(entries) >= 2000:
                truncated = True
                break
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
            except OSError:
                continue
            entry_path = Path(entry.path).resolve()
            if self.allow_root is not None and not _is_within(entry_path, self.allow_root):
                continue
            entries.append({"name": entry.name, "path": str(entry_path)})
        entries.sort(key=lambda item: item["name"].casefold())

        parent: str | None = None
        if path.parent != path:
            candidate = path.parent
            if self.allow_root is None or _is_within(candidate, self.allow_root):
                parent = str(candidate)

        roots: list[dict[str, str]] = []
        if self.allow_root is not None:
            roots.append({"path": str(self.allow_root), "label": "扫描根目录"})
        else:
            for candidate, label in ((Path.home(), "主目录"), (Path.cwd(), "当前目录"), (Path("/"), "根目录")):
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.is_dir() and all(item["path"] != str(resolved) for item in roots):
                    roots.append({"path": str(resolved), "label": label})
        return {
            "current": str(path),
            "parent": parent,
            "entries": entries,
            "roots": roots,
            "allow_root": str(self.allow_root) if self.allow_root else None,
            "truncated": truncated,
        }

    def is_thumbnail_path_allowed(self, value: str) -> Path | None:
        try:
            path = Path(value).expanduser().resolve()
        except OSError:
            return None
        if not path.is_file():
            return None
        if self.allow_root is not None and not _is_within(path, self.allow_root):
            return None
        for root in self.report_roots():
            if _is_within(path, root):
                return path
        return None

    # ------------------------------------------------------------------ cache
    def cache_stats(self) -> dict[str, Any]:
        with CacheStore(self.db_path) as store:
            return store.stats()

    def cache_prune(self) -> dict[str, Any]:
        with CacheStore(self.db_path) as store:
            removed = store.prune_missing()
            return {"removed": removed, "stats": store.stats()}

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _parse_types(value: Any) -> set[str] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError("types must be a list of strings")
        return {item.strip().lower() for item in value if item.strip()}

    @staticmethod
    def _parse_optional_int(value: Any, name: str, *, minimum: int) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return parsed


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
