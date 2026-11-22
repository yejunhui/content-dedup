"""Shared pytest fixtures and synthetic content builders."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from dedup.adapters import AdapterRegistry
from dedup.config import AppConfig
from dedup.pipeline import Scanner
from dedup.store import CacheStore


def make_image(
    path: Path,
    *,
    color: tuple[int, int, int] = (220, 40, 40),
    background: tuple[int, int, int] = (20, 30, 40),
    text: str = "Hello",
    size: tuple[int, int] = (640, 480),
    quality: int | None = None,
) -> Path:
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.ellipse(
        (int(width * 0.12), int(height * 0.12), int(width * 0.88), int(height * 0.88)),
        fill=color,
    )
    draw.rectangle(
        (int(width * 0.28), int(height * 0.30), int(width * 0.72), int(height * 0.70)),
        fill=(240, 240, 230),
    )
    draw.text((int(width * 0.30), int(height * 0.38)), text, fill=(10, 10, 10))
    if quality is None:
        image.save(path)
    else:
        image.convert("RGB").save(path, quality=quality)
    return path


def make_near_image(source: Path, target: Path, *, quality: int = 45) -> Path:
    with Image.open(source) as image:
        width, height = image.size
        resized = image.resize((int(width * 0.9), int(height * 0.9)), Image.Resampling.LANCZOS)
        resized.save(target, quality=quality)
    return target


def make_text(path: Path, phrase: str = "alpha beta gamma delta epsilon zeta eta theta iota kappa", repeats: int = 40) -> Path:
    path.write_text((" ".join([phrase] * repeats) + "\n"), encoding="utf-8")
    return path


def make_docx(path: Path, text: str = "A document with enough words for a stable SimHash fingerprint. " * 20, title: str = "Title") -> Path:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "left"
    table.rows[0].cells[1].text = "right"
    document.core_properties.title = title
    document.save(path)
    return path


def make_xlsx(path: Path, values: list[list[object]] | None = None) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    values = values or [
        ["name", "value", "comment"],
        ["alpha", 1, "first row with enough descriptive text"],
        ["beta", 2, "second row with even more descriptive text"],
        ["gamma", 3, "third row with additional descriptive text"],
        ["delta", 4, "fourth row with more descriptive text"],
    ]
    for row in values:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return path


def make_zip(path: Path, entries: dict[str, str], *, compression: int = 8) -> Path:
    import zipfile

    mode = zipfile.ZIP_DEFLATED if compression else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", mode) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def make_pdf(path: Path, text: str = "A short PDF document with enough extractable words for a stable SimHash fingerprint and reliable deduplication across documents exported from different tools and versions. ") -> Path:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    parts: list[bytes] = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = sum(len(part) for part in parts)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode())
    trailer = (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    path.write_bytes(b"".join(parts + xref + [trailer]))
    return path


def make_video(path: Path, pattern: str = "testsrc", duration: int = 6, size: str = "160x120") -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"{pattern}=duration={duration}:size={size}:rate=10",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-crf",
        "28",
        str(path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip(f"cannot create test video: {result.stderr[-300:]}")
    return path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig()


@pytest.fixture
def registry(app_config: AppConfig) -> AdapterRegistry:
    return AdapterRegistry(app_config)


@pytest.fixture
def run_scan(tmp_path: Path):
    def _run(
        root: Path,
        *,
        config: AppConfig | None = None,
        mode: str = "all",
        types: set[str] | None = None,
        refresh: bool = False,
        limit: int | None = None,
        db: Path | None = None,
    ):
        config = config or AppConfig()
        database = db or (tmp_path / "cache.sqlite3")
        registry = AdapterRegistry(config)
        with CacheStore(database) as store:
            scanner = Scanner(config, store, registry)
            return scanner.scan(root, mode=mode, types=types, refresh=refresh, limit=limit)

    return _run


@pytest.fixture
def set_mtime():
    def _set(path: Path, mtime_ns: int) -> None:
        os.utime(path, ns=(mtime_ns, mtime_ns))

    return _set
