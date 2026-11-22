from __future__ import annotations

from pathlib import Path

import pytest

from dedup.adapters.base import UnsupportedFingerprint
from dedup.config import AppConfig

from .conftest import make_text


def test_missing_fpcalc_degrades_to_exact_only(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "one.mp3").write_bytes(b"not really an mp3 but bytes are enough")
    (root / "two.mp3").write_bytes(b"not really an mp3 but bytes are enough")
    config = AppConfig()
    config.audio.fpcalc = "definitely-not-a-real-fpcalc-binary"
    result = run_scan(root, types={"audio"}, config=config)
    assert len(result.records) == 2
    assert all(record.fp_status == "unsupported" for record in result.records)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "exact"


def test_audio_adapter_reports_missing_binary(app_config: AppConfig, registry, tmp_path: Path) -> None:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"bytes")
    adapter = registry.get("audio")
    adapter.config.audio.fpcalc = "definitely-not-a-real-fpcalc-binary"
    with pytest.raises(UnsupportedFingerprint):
        adapter.compute_fingerprint(path)


def test_text_and_audio_do_not_cross_match(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "text.txt")
    (root / "audio.mp3").write_bytes(b"same-ish content")
    result = run_scan(root)
    assert all(
        not (group.keeper.adapter == "text" and any(d.adapter == "audio" for d in group.duplicates))
        for group in result.groups
    )


def test_audio_fpcalc_json_and_grouping(run_scan, tmp_path: Path) -> None:
    script = tmp_path / "fake_fpcalc.py"
    script.write_text(
        """#!/usr/bin/env python3
import json
import sys

data = open(sys.argv[-1], "rb").read()
base = [((index * 2654435761) & 0xFFFFFFFF) for index in range(120)]
tweak = data[0] if data else 0
values = list(base)
if tweak < 32:
    for index in range(tweak):
        values[index] ^= 1
else:
    values = [value ^ 0xFFFFFFFF for value in values]
print(json.dumps({"duration": 10.0, "fingerprint": values}))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)

    root = tmp_path / "data"
    root.mkdir()
    (root / "one.mp3").write_bytes(b"\x00" + b"same audio bytes")
    (root / "two.mp3").write_bytes(b"\x01" + b"same audio bytes")
    (root / "other.mp3").write_bytes(b"\xff" + b"same audio bytes")
    config = AppConfig()
    config.audio.fpcalc = str(script)
    result = run_scan(root, types={"audio"}, config=config)
    assert all(record.fp_status == "ok" for record in result.records)
    groups = [group for group in result.groups if group.kind == "audio:chromaprint"]
    assert len(groups) == 1
    paths = {groups[0].keeper.path, *(item.path for item in groups[0].duplicates)}
    assert str(root / "one.mp3") in paths
    assert str(root / "two.mp3") in paths
    assert str(root / "other.mp3") not in paths
