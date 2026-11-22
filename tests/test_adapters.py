from __future__ import annotations

from pathlib import Path

import pytest

from dedup.adapters.base import UnsupportedFingerprint
from dedup.config import AppConfig
from dedup.grouping import keeper_key

from .conftest import (
    make_docx,
    make_image,
    make_near_image,
    make_pdf,
    make_text,
    make_xlsx,
    make_zip,
)


def test_image_near_duplicate_matches(app_config: AppConfig, registry, tmp_path: Path) -> None:
    base = make_image(tmp_path / "base.png")
    near = make_near_image(base, tmp_path / "near.jpg")
    adapter = registry.get("image")
    left = adapter.compute_fingerprint(base)
    right = adapter.compute_fingerprint(near)
    kind = app_config.image.algorithm
    info = adapter.match(kind, left[kind], right[kind])
    assert info is not None and info.similar


def test_image_color_mismatch_rejected(app_config: AppConfig, registry, tmp_path: Path) -> None:
    red = make_image(tmp_path / "red.png", color=(220, 40, 40))
    blue = make_image(tmp_path / "blue.png", color=(30, 120, 220))
    adapter = registry.get("image")
    left = adapter.compute_fingerprint(red)
    right = adapter.compute_fingerprint(blue)
    info = adapter.match("phash", left["phash"], right["phash"])
    assert info is not None
    assert not info.similar


def test_text_adapter_near_and_far(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("text")
    first = make_text(tmp_path / "one.txt")
    second = make_text(tmp_path / "two.txt", repeats=40)
    second.write_text(second.read_text(encoding="utf-8") + "extra tail", encoding="utf-8")
    far = make_text(
        tmp_path / "far.txt",
        phrase="unrelated music painting travel history science",
        repeats=40,
    )
    one = adapter.compute_fingerprint(first)["simhash"]
    two = adapter.compute_fingerprint(second)["simhash"]
    other = adapter.compute_fingerprint(far)["simhash"]
    assert adapter.match("simhash", one, two).similar
    assert not adapter.match("simhash", one, other).similar


def test_docx_same_text_different_metadata(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("document")
    first = make_docx(tmp_path / "one.docx", title="First")
    second = make_docx(tmp_path / "two.docx", title="Second")
    one = adapter.compute_fingerprint(first)["simhash"]
    two = adapter.compute_fingerprint(second)["simhash"]
    assert adapter.match("simhash", one, two).similar


def test_xlsx_same_values(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("document")
    first = make_xlsx(tmp_path / "one.xlsx")
    second = make_xlsx(tmp_path / "two.xlsx")
    one = adapter.compute_fingerprint(first)["simhash"]
    two = adapter.compute_fingerprint(second)["simhash"]
    assert adapter.match("simhash", one, two).similar


def test_pdf_extracts_text(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("document")
    path = make_pdf(tmp_path / "document.pdf")
    fingerprint = adapter.compute_fingerprint(path)["simhash"]
    assert fingerprint.meta["extractor"] == "pypdf"
    assert fingerprint.meta["tokens"] >= 5


def test_legacy_office_is_unsupported(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("document")
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0fake")
    with pytest.raises(UnsupportedFingerprint):
        adapter.compute_fingerprint(path)


def test_archive_repack_matches(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("archive")
    entries = {"a.txt": "hello world", "b/c.txt": "second file"}
    first = make_zip(tmp_path / "one.zip", entries, compression=8)
    second = make_zip(
        tmp_path / "two.zip",
        {"b/c.txt": entries["b/c.txt"], "a.txt": entries["a.txt"]},
        compression=0,
    )
    one = adapter.compute_fingerprint(first)["manifest"]
    two = adapter.compute_fingerprint(second)["manifest"]
    assert one.value == two.value
    assert adapter.match("manifest", one, two).similar


def test_archive_different_content_no_match(app_config: AppConfig, registry, tmp_path: Path) -> None:
    adapter = registry.get("archive")
    first = make_zip(tmp_path / "one.zip", {"a.txt": "hello"})
    second = make_zip(tmp_path / "two.zip", {"a.txt": "goodbye"})
    one = adapter.compute_fingerprint(first)["manifest"]
    two = adapter.compute_fingerprint(second)["manifest"]
    assert one.value != two.value
    assert not adapter.match("manifest", one, two).similar


def test_tar_symlink_target_changes_manifest(app_config: AppConfig, registry, tmp_path: Path) -> None:
    import io
    import tarfile

    adapter = registry.get("archive")

    def make_tar(path: Path, link_target: str) -> Path:
        with tarfile.open(path, "w") as archive:
            data = b"payload"
            info = tarfile.TarInfo("data.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = link_target
            archive.addfile(link)
        return path

    first = make_tar(tmp_path / "one.tar", "data.txt")
    second = make_tar(tmp_path / "two.tar", "other.txt")
    one = adapter.compute_fingerprint(first)["manifest"]
    two = adapter.compute_fingerprint(second)["manifest"]
    assert one.value != two.value


def test_svg_is_exact_only(app_config: AppConfig, registry, tmp_path: Path) -> None:
    assert registry.resolve(tmp_path / "drawing.svg").name == "binary"


def test_binary_adapter_exact_only(app_config: AppConfig, registry) -> None:
    adapter = registry.get("binary")
    assert adapter.fingerprint_kinds == ()
    assert not adapter.supports_fingerprint


def test_keeper_policies() -> None:
    from dedup.models import FileRecord

    older = FileRecord("b", 100, 100, "text", "hash1")
    newer = FileRecord("a", 50, 200, "text", "hash2")
    assert keeper_key(older, "oldest") < keeper_key(newer, "oldest")
    assert keeper_key(newer, "newest") < keeper_key(older, "newest")
    assert keeper_key(older, "largest") < keeper_key(newer, "largest")
    assert keeper_key(older, "shortest_path")[0] == 1
