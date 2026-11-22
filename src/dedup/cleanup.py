"""Safe cleanup: move reported duplicates into a trash directory.

The CLI applies a whole report; the web UI can apply a per-group selection with
a custom keeper. Both paths share the same verification and journal logic.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .util import (
    atomic_write_json,
    safe_relpath,
    timestamp_slug,
    unique_path,
    utc_now_iso,
)

logger = logging.getLogger("dedup.cleanup")

ApplyProgressCallback = Callable[[dict[str, Any]], None]


class VerificationError(RuntimeError):
    pass


def apply_report(
    report: dict[str, Any],
    *,
    trash_dir: str | Path | None = None,
    dry_run: bool = True,
    verify: bool = True,
    progress_callback: ApplyProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Apply all groups in a report (CLI behaviour)."""
    selections: list[dict[str, Any]] = []
    for group in report.get("groups") or []:
        keeper = group.get("keeper")
        duplicates = group.get("duplicates") or []
        if not keeper or not duplicates:
            continue
        selections.append(
            {
                "group_id": group.get("id", ""),
                "keeper": keeper.get("path", ""),
                "duplicates": [item.get("path", "") for item in duplicates],
            }
        )
    return apply_selections(
        report,
        selections,
        trash_dir=trash_dir,
        dry_run=dry_run,
        verify=verify,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def apply_selections(
    report: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    trash_dir: str | Path | None = None,
    dry_run: bool = True,
    verify: bool = True,
    progress_callback: ApplyProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Move explicitly selected duplicates, optionally changing the keeper.

    Each selection has the shape::

        {"group_id": "g000001",
         "keeper": "/data/keep.jpg",
         "duplicates": ["/data/copy1.jpg", "/data/copy2.jpg"]}
    """
    if not isinstance(report, dict) or not report.get("root"):
        raise ValueError("invalid report: missing root")
    if not isinstance(selections, list):
        raise TypeError("selections must be a list")

    root = Path(report["root"]).expanduser().resolve()
    settings = report.get("settings") or {}
    safety = settings.get("safety") or {}
    if trash_dir is None:
        configured = Path(str(safety.get("trash_dir") or ".dedup-trash")).expanduser()
        base = configured if configured.is_absolute() else root / configured
        trash_root = base / timestamp_slug()
    else:
        trash_root = Path(trash_dir).expanduser().resolve()
    trash_root = trash_root.resolve()

    groups_by_id: dict[str, dict[str, Any]] = {}
    for group in report.get("groups") or []:
        group_id = str(group.get("id", ""))
        if group_id:
            groups_by_id[group_id] = group

    selected: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    selected_group_ids: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise TypeError("each selection must be an object")
        group_id = str(selection.get("group_id", ""))
        group = groups_by_id.get(group_id)
        if group is None:
            raise ValueError(f"group not found in report: {group_id}")
        if group_id in selected_group_ids:
            raise ValueError(f"duplicate selection for group: {group_id}")
        keeper_path = str(selection.get("keeper", ""))
        duplicate_paths = selection.get("duplicates") or []
        if not isinstance(duplicate_paths, list):
            raise TypeError(f"duplicates must be a list for group {group_id}")
        members: dict[str, dict[str, Any]] = {}
        keeper_member = group.get("keeper")
        if keeper_member:
            members[str(keeper_member.get("path", ""))] = keeper_member
        for member in group.get("duplicates") or []:
            members[str(member.get("path", ""))] = member
        if keeper_path not in members:
            raise ValueError(f"keeper is not part of group {group_id}: {keeper_path}")
        duplicates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in duplicate_paths:
            path = str(path)
            if path == keeper_path:
                raise ValueError("keeper cannot also be a duplicate")
            member = members.get(path)
            if member is None:
                raise ValueError(f"file is not part of group {group_id}: {path}")
            if path not in seen:
                seen.add(path)
                duplicates.append(member)
        if duplicates:
            selected.append((group, members[keeper_path], duplicates))
            selected_group_ids.add(group_id)

    hash_check = bool(verify) and bool(safety.get("verify_on_apply", True))
    total = sum(len(duplicates) for _, _, duplicates in selected)
    progress_state = {"processed": 0}
    warnings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "root": str(root),
        "trash": str(trash_root),
        "dry_run": dry_run,
        "groups": len(selected),
        "selected_groups": [str(group.get("id", "")) for group, _, _ in selected],
        "total_selected": total,
        "moved": 0,
        "planned": 0,
        "skipped": 0,
        "errors": 0,
        "reclaimable_bytes": 0,
        "cancelled": False,
        "hash_check": hash_check,
        "items": [],
        "warnings": warnings,
        "manifest": None,
    }

    def emit(message: str, current: str | None = None, *, phase: str = "applying") -> None:
        if progress_callback is None:
            return
        processed = progress_state["processed"]
        percent = round(min(100.0, processed / total * 100.0), 1) if total else 0.0
        try:
            progress_callback(
                {
                    "phase": phase,
                    "processed": processed,
                    "total": total,
                    "percent": percent,
                    "message": message,
                    "current": current,
                }
            )
        except Exception:
            logger.debug("apply progress callback failed", exc_info=True)

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    emit("准备移动到回收站…", phase="starting")
    manifest_entries: list[dict[str, Any]] = []
    for group, keeper, duplicates in selected:
        if is_cancelled():
            summary["cancelled"] = True
            break
        try:
            _verify_member(keeper, hash_check=hash_check)
        except VerificationError as exc:
            summary["skipped"] += len(duplicates)
            warnings.append(
                {"path": keeper.get("path", ""), "reason": f"keeper verification failed: {exc}"}
            )
            for _ in duplicates:
                progress_state["processed"] += 1
            emit(f"跳过一组：保留文件校验失败（{exc}）", keeper.get("path"))
            continue

        for duplicate in duplicates:
            if is_cancelled():
                summary["cancelled"] = True
                break
            source = Path(str(duplicate.get("path", "")))
            try:
                _verify_member(duplicate, hash_check=hash_check)
            except VerificationError as exc:
                summary["skipped"] += 1
                warnings.append(
                    {"path": str(source), "reason": f"verification failed: {exc}"}
                )
            else:
                if _same_path(source, keeper.get("path", "")):
                    summary["skipped"] += 1
                    warnings.append(
                        {"path": str(source), "reason": "duplicate path equals keeper path"}
                    )
                else:
                    relative = str(duplicate.get("rel_path") or safe_relpath(source, root))
                    relative_path = Path(relative)
                    if relative_path.is_absolute() or ".." in relative_path.parts:
                        summary["skipped"] += 1
                        warnings.append(
                            {"path": str(source), "reason": "unsafe relative path in report"}
                        )
                    else:
                        destination = unique_path(trash_root / relative_path)
                        item = {
                            "group": group.get("id", ""),
                            "source": str(source),
                            "destination": str(destination),
                            "size": int(duplicate.get("size") or 0),
                            "reason": duplicate.get("reason", ""),
                            "distance": duplicate.get("distance"),
                            "score": duplicate.get("score"),
                        }
                        if dry_run:
                            summary["planned"] += 1
                            summary["reclaimable_bytes"] += item["size"]
                            summary["items"].append(item)
                        else:
                            try:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                if destination.exists():
                                    raise VerificationError(
                                        f"destination already exists: {destination}"
                                    )
                                _append_journal(
                                    trash_root,
                                    {**item, "status": "moving", "at": utc_now_iso()},
                                )
                                shutil.move(str(source), str(destination))
                            except Exception as exc:  # noqa: BLE001 - continue with other files
                                summary["errors"] += 1
                                warnings.append(
                                    {"path": str(source), "reason": f"move failed: {exc}"}
                                )
                            else:
                                summary["moved"] += 1
                                summary["reclaimable_bytes"] += item["size"]
                                summary["items"].append(item)
                                manifest_entries.append(
                                    {
                                        **item,
                                        "moved_at": utc_now_iso(),
                                        "original_mtime_ns": int(
                                            duplicate.get("mtime_ns") or 0
                                        ),
                                        "exact_hash": duplicate.get("exact_hash", ""),
                                    }
                                )
                                try:
                                    _append_journal(
                                        trash_root,
                                        {**manifest_entries[-1], "status": "moved"},
                                    )
                                except OSError as exc:
                                    warnings.append(
                                        {
                                            "path": str(source),
                                            "reason": f"journal update failed: {exc}",
                                        }
                                    )
                                logger.info("moved %s -> %s", source, destination)
            finally:
                progress_state["processed"] += 1
                if not is_cancelled():
                    processed = progress_state["processed"]
                    percent = (
                        round(min(100.0, processed / total * 100.0), 1) if total else 0.0
                    )
                    if progress_callback is not None:
                        try:
                            progress_callback(
                                {
                                    "phase": "applying",
                                    "processed": processed,
                                    "total": total,
                                    "percent": percent,
                                    "message": f"已处理 {processed}/{total}",
                                    "current": str(source),
                                }
                            )
                        except Exception:
                            logger.debug("apply progress callback failed", exc_info=True)
        if summary["cancelled"]:
            break

    if summary["cancelled"]:
        summary["cancelled"] = True
        emit("已取消；已完成的操作会保留在回收站中", phase="cancelled")
    if not dry_run and manifest_entries:
        manifest = {
            "tool": "content-dedup",
            "created_at": utc_now_iso(),
            "root": str(root),
            "trash": str(trash_root),
            "entries": manifest_entries,
        }
        manifest_path = trash_root / "manifest.json"
        try:
            atomic_write_json(manifest_path, manifest)
            summary["manifest"] = str(manifest_path)
        except OSError as exc:
            summary["errors"] += 1
            warnings.append(
                {"path": str(manifest_path), "reason": f"manifest write failed: {exc}"}
            )
    if total and not summary["cancelled"]:
        emit("清理完成", phase="done")
    return summary


def _same_path(left: Path, right: str | Path) -> bool:
    try:
        return left.resolve() == Path(str(right)).expanduser().resolve()
    except OSError:
        return False


def _verify_member(member: dict[str, Any], *, hash_check: bool) -> None:
    path = Path(str(member.get("path", "")))
    if not path.exists():
        raise VerificationError("file is missing")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerificationError(f"cannot stat file: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise VerificationError("refusing to move a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError("not a regular file")
    expected_size = int(member.get("size") or 0)
    if info.st_size != expected_size:
        raise VerificationError(
            f"size changed ({info.st_size} != {expected_size}); rescan required"
        )
    expected_mtime = int(member.get("mtime_ns") or 0)
    if expected_mtime and info.st_mtime_ns != expected_mtime:
        raise VerificationError("mtime changed; rescan required")
    expected_hash = str(member.get("exact_hash") or "")
    if hash_check and expected_hash:
        from .util import sha256_file

        try:
            actual = sha256_file(path)
        except OSError as exc:
            raise VerificationError(f"cannot re-read file: {exc}") from exc
        if actual != expected_hash:
            raise VerificationError("content hash changed; rescan required")


def _append_journal(trash_root: Path, entry: dict[str, Any]) -> None:
    trash_root.mkdir(parents=True, exist_ok=True)
    journal = trash_root / "journal.jsonl"
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
