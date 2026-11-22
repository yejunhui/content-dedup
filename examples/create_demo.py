#!/usr/bin/env python3
"""Create a small demo library that shows ContentDedup's value.

Usage:
    python examples/create_demo.py ./demo-content
    dedup web --root ./demo-content --open
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

PHRASE = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
    "content deduplication safe preview recycle bin "
)


def image(path: Path, color: tuple[int, int, int], label: str) -> None:
    picture = Image.new("RGB", (900, 650), (23, 30, 44))
    draw = ImageDraw.Draw(picture)
    draw.ellipse((90, 70, 810, 580), fill=color)
    draw.rectangle((250, 190, 650, 470), fill=(247, 244, 234))
    draw.text((320, 280), label, fill=(15, 18, 28))
    picture.save(path)


def make_zip(path: Path, *, compression: int, reverse: bool) -> None:
    entries = [
        ("readme.txt", "content dedup demo payload\n"),
        ("data/list.csv", "name,value\nalpha,1\nbeta,2\n"),
        ("photos/readme.txt", "same bytes, different archive\n"),
    ]
    if reverse:
        entries.reverse()
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)


def make_video(path: Path, copy: Path | None = None) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    first = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=6:size=320x240:rate=12",
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
    )
    if first.returncode != 0:
        return False
    if copy is not None:
        second = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-vf",
                "scale=256:192",
                "-c:v",
                "libx264",
                "-crf",
                "36",
                "-preset",
                "veryfast",
                str(copy),
            ],
            capture_output=True,
            check=False,
        )
        return second.returncode == 0
    return True


def make_docx(path: Path, text: str) -> bool:
    try:
        from docx import Document
    except ImportError:
        return False
    document = Document()
    document.add_paragraph(text * 20)
    document.save(path)
    return True


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-content").expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        print(f"Refusing to overwrite non-empty directory: {target}", file=sys.stderr)
        return 2
    (target / "photos").mkdir(parents=True, exist_ok=True)
    (target / "docs").mkdir(parents=True, exist_ok=True)
    (target / "archives").mkdir(parents=True, exist_ok=True)
    (target / "video").mkdir(parents=True, exist_ok=True)

    image(target / "photos" / "beach.png", (225, 82, 58), "Beach")
    with Image.open(target / "photos" / "beach.png") as source:
        source.resize((820, 590)).save(target / "photos" / "beach-small.jpg", quality=42)
    image(target / "photos" / "city.png", (35, 118, 205), "City")

    (target / "docs" / "notes.txt").write_text((PHRASE * 40).strip(), encoding="utf-8")
    (target / "docs" / "notes-copy.txt").write_text(
        (PHRASE * 40).strip() + " extra tail", encoding="utf-8"
    )
    make_docx(target / "docs" / "report.docx", "Quarterly report revenue growth operating costs customer retention product roadmap. ")
    make_docx(target / "docs" / "report-copy.docx", "Quarterly report revenue growth operating costs customer retention product roadmap. ")

    make_zip(target / "archives" / "backup1.zip", compression=zipfile.ZIP_DEFLATED, reverse=False)
    make_zip(target / "archives" / "backup2.zip", compression=zipfile.ZIP_STORED, reverse=True)

    video_ok = make_video(
        target / "video" / "holiday.mp4",
        target / "video" / "holiday-copy.mp4",
    )

    print(f"Demo library created: {target}")
    print("Contains duplicate/near-duplicate photos, text, DOCX and archives" + (" and video" if video_ok else "") + ".")
    print()
    print("Try it with the web UI:")
    print(f"  dedup web --root {target} --open")
    print()
    print("Or with the CLI:")
    print(f"  dedup scan {target} --report ./demo-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
