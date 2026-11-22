from __future__ import annotations

import json
from pathlib import Path

from dedup.cli import main

from .conftest import make_text


def test_cli_scan_report_and_dry_run_apply(tmp_path: Path, capsys) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    two = make_text(root / "two.txt")
    two.write_text(two.read_text(encoding="utf-8") + "extra tail", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("scan:\n  workers: 1\n", encoding="utf-8")
    db_path = tmp_path / "cache.sqlite3"
    report_path = tmp_path / "report.json"

    code = main(
        [
            "scan",
            str(root),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--report",
            str(report_path),
            "--types",
            "text",
        ]
    )
    assert code == 0
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["groups"] == 1

    code = main(["report", "--report", str(report_path), "--top", "5"])
    assert code == 0
    output = capsys.readouterr().out
    assert "ContentDedup report" in output
    assert "text:simhash" in output

    code = main(["apply", "--report", str(report_path)])
    assert code == 0
    output = capsys.readouterr().out
    assert "Dry run only" in output
    assert (root / "one.txt").exists() and (root / "two.txt").exists()

    code = main(
        ["cache", "--config", str(config_path), "--db", str(db_path), "stats"]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert '"files": 2' in output
