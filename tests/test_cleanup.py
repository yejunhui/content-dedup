from __future__ import annotations

import os
from pathlib import Path

from dedup.cleanup import apply_report
from dedup.config import AppConfig
from dedup.report import write_report

from .conftest import make_text


def _report_for(root: Path, tmp_path: Path) -> tuple[dict, Path, Path]:
    from dedup.adapters import AdapterRegistry
    from dedup.pipeline import Scanner
    from dedup.store import CacheStore

    config = AppConfig()
    with CacheStore(tmp_path / "cache.sqlite3") as store:
        result = Scanner(config, store, AdapterRegistry(config)).scan(root)
    json_path, _ = write_report(result, config, tmp_path / "report.json")
    import json

    report = json.loads(json_path.read_text(encoding="utf-8"))
    group = report["groups"][0]
    keeper = Path(group["keeper"]["path"])
    duplicate = Path(group["duplicates"][0]["path"])
    return report, keeper, duplicate


def test_dry_run_does_not_move(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    report, keeper, duplicate = _report_for(root, tmp_path)
    summary = apply_report(report, dry_run=True)
    assert summary["planned"] == 1
    assert summary["moved"] == 0
    assert keeper.exists() and duplicate.exists()
    assert summary["trash"].startswith(str(root))


def test_apply_moves_duplicate_and_writes_manifest(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    report, keeper, duplicate = _report_for(root, tmp_path)
    summary = apply_report(report, dry_run=False)
    assert summary["moved"] == 1
    assert summary["errors"] == 0
    assert keeper.exists()
    assert not duplicate.exists()
    assert summary["manifest"] is not None
    manifest = Path(summary["manifest"])
    assert manifest.is_file()
    moved = list((Path(summary["trash"])).rglob("*.txt"))
    assert len(moved) == 1


def test_apply_rejects_changed_content(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    report, _keeper, duplicate = _report_for(root, tmp_path)
    original_stat = duplicate.stat()
    payload = bytearray(duplicate.read_bytes())
    payload[:100] = b"X" * 100
    duplicate.write_bytes(bytes(payload))
    os.utime(duplicate, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    summary = apply_report(report, dry_run=False)
    assert summary["moved"] == 0
    assert summary["skipped"] >= 1
    assert duplicate.exists()


def test_apply_rejects_missing_keeper(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    report, keeper, duplicate = _report_for(root, tmp_path)
    keeper.unlink()
    summary = apply_report(report, dry_run=False)
    assert summary["moved"] == 0
    assert duplicate.exists()
    assert any("keeper" in warning["reason"] for warning in summary["warnings"])
