from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dedup.config import AppConfig

from .conftest import make_video

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg missing")
def test_video_reencode_is_grouped(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    first = make_video(root / "first.mp4", "testsrc")
    second = root / "second.mp4"
    result = subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-i",
            str(first),
            "-vf",
            "scale=144:108",
            "-c:v",
            "libx264",
            "-crf",
            "38",
            str(second),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot re-encode video: {result.stderr[-300:]}")
    make_video(root / "other.mp4", "testsrc2")
    scan = run_scan(root, types={"video"})
    assert len(scan.records) == 3
    groups = [group for group in scan.groups if group.kind == "video:phash_seq"]
    assert len(groups) == 1
    paths = {groups[0].keeper.path, *(item.path for item in groups[0].duplicates)}
    assert str(first) in paths
    assert str(second) in paths
    assert str(root / "other.mp4") not in paths


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_video_duration_gate(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_video(root / "short.mp4", "testsrc", duration=3)
    make_video(root / "long.mp4", "testsrc", duration=8)
    scan = run_scan(root, types={"video"}, config=AppConfig())
    assert scan.groups == []


def _make_color_video(path: Path, color: str) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x120:d=4:r=10",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-crf",
            "28",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create color video: {result.stderr[-300:]}")
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_video_static_colors_are_not_grouped(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _make_color_video(root / "red.mp4", "red")
    _make_color_video(root / "blue.mp4", "blue")
    scan = run_scan(root, types={"video"}, config=AppConfig())
    assert scan.groups == []
