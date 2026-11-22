from __future__ import annotations

from pathlib import Path

from dedup.config import AppConfig
from dedup.models import FileRecord, Fingerprint
from dedup.report import build_report, load_report, write_report

from .conftest import make_image, make_near_image, make_text


def test_exact_mode_skips_fingerprints(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    base = make_image(root / "base.png")
    make_near_image(base, root / "near.jpg")
    result = run_scan(root, mode="exact")
    assert result.layers == ["exact"]
    assert result.groups == []
    assert all(record.fp_status == "none" for record in result.records)


def test_all_mode_groups_near_images(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    base = make_image(root / "base.png")
    make_near_image(base, root / "near.jpg")
    make_image(root / "other.png", color=(30, 120, 220))
    result = run_scan(root, mode="all")
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.kind == "image:phash"
    paths = {group.keeper.path, *(duplicate.path for duplicate in group.duplicates)}
    assert str(base) in paths
    assert str(root / "near.jpg") in paths
    assert str(root / "other.png") not in paths


def test_keeper_oldest_policy(run_scan, set_mtime, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    base = make_image(root / "base.png")
    near = make_near_image(base, root / "near.jpg")
    set_mtime(base, 1_000_000_000_000_000_000)
    set_mtime(near, 1_100_000_000_000_000_000)
    result = run_scan(root, mode="all")
    assert len(result.groups) == 1
    assert result.groups[0].keeper.path == str(base)


def test_incremental_cache(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(3):
        (root / f"copy{index}.bin").write_bytes(b"identical bytes" * 100)
    (root / "unique.bin").write_bytes(b"different bytes" * 100)
    first = run_scan(root)
    assert first.stats.cache_hits == 0
    assert len(first.groups) == 1
    second = run_scan(root)
    assert second.stats.cache_hits == len(second.records)
    assert len(second.groups) == len(first.groups)


def test_exact_hash_reused_by_fingerprint_scan(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    exact = run_scan(root, mode="exact")
    assert exact.stats.exact_reused == 0
    all_layers = run_scan(root, mode="all")
    assert all_layers.stats.cache_hits == 0
    assert all_layers.stats.exact_reused == len(all_layers.records)
    assert len(all_layers.groups) == 1
    cached = run_scan(root, mode="all")
    assert cached.stats.cache_hits == len(cached.records)
    assert cached.stats.exact_reused == 0


def test_incremental_recomputes_after_change(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    one = root / "one.bin"
    two = root / "two.bin"
    one.write_bytes(b"same" * 100)
    two.write_bytes(b"same" * 100)
    first = run_scan(root)
    assert len(first.groups) == 1
    two.write_bytes(b"changed" * 100)
    second = run_scan(root)
    assert second.stats.cache_hits == 1
    assert second.groups == []


def test_type_filter(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_image(root / "image.png")
    make_text(root / "text.txt")
    result = run_scan(root, types={"text"})
    assert len(result.records) == 1
    assert result.records[0].adapter == "text"
    assert result.stats.skipped_type == 1


def test_limit(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(3):
        (root / f"copy{index}.bin").write_bytes(b"x" * 1000)
    result = run_scan(root, limit=2)
    assert len(result.records) == 2
    assert result.stats.files_selected == 2


def test_parallel_workers(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(4):
        (root / f"copy{index}.txt").write_bytes(b"identical text " * 200)
    make_image(root / "one.png")
    make_image(root / "two.png", color=(30, 120, 220))
    config = AppConfig()
    config.scan.workers = 2
    result = run_scan(root, config=config)
    assert result.stats.files_processed == 6
    assert len(result.groups) == 1
    assert len(result.groups[0].duplicates) == 3


def test_store_prune_and_clear(tmp_path: Path) -> None:
    from dedup.store import CacheStore

    existing = tmp_path / "exists.bin"
    existing.write_bytes(b"data")
    with CacheStore(tmp_path / "cache.sqlite3") as store:
        for path, size in ((existing, 4), (tmp_path / "missing.bin", 5)):
            store.put(
                FileRecord(
                    path=str(path),
                    size=size,
                    mtime_ns=1,
                    adapter="binary",
                    exact_hash=path.name,
                    fp_status="unsupported",
                ),
                commit=True,
            )
        assert store.stats()["files"] == 2
        assert store.prune_missing() == 1
        assert store.stats()["files"] == 1
        assert store.clear() == 1
        assert store.stats()["files"] == 0


def test_report_write_and_issues(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    two = make_text(root / "two.txt")
    two.write_text(two.read_text(encoding="utf-8") + "extra tail", encoding="utf-8")
    (root / "short.txt").write_text("too short", encoding="utf-8")
    result = run_scan(root)
    report = build_report(result, AppConfig())
    assert report["summary"]["groups"] == 1
    assert report["stats"]["fingerprints_unsupported"] >= 1
    assert any(issue["adapter"] == "text" for issue in report["fingerprint_issues"])
    json_path, md_path = write_report(
        result,
        AppConfig(),
        tmp_path / "report.json",
    )
    assert json_path.is_file() and md_path.is_file()
    loaded = load_report(json_path)
    assert loaded["summary"]["groups"] == 1
    markdown = md_path.read_text(encoding="utf-8")
    assert "text:simhash" in markdown


def test_exact_duplicate_across_adapters(run_scan, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    payload = b"same bytes " * 200
    (root / "a.txt").write_bytes(payload)
    (root / "b.bin").write_bytes(payload)
    result = run_scan(root)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "exact"
    assert len(result.groups[0].duplicates) == 1


def test_store_fingerprint_roundtrip(tmp_path: Path) -> None:
    from dedup.store import CacheStore

    with CacheStore(tmp_path / "cache.sqlite3") as store:
        record = FileRecord(
            path=str(tmp_path / "file.txt"),
            size=10,
            mtime_ns=20,
            adapter="text",
            exact_hash="abc",
            fingerprints={
                "simhash": Fingerprint("simhash", 2, "0000000000000001", {"tokens": 10})
            },
            fp_status="ok",
            fp_sig="sig",
        )
        store.put(record, commit=True)
        cached = store.get(
            record.path,
            10,
            20,
            "text",
            require_fingerprint=True,
            expected_kinds=("simhash",),
            fp_sig="sig",
        )
        assert cached is not None
        assert cached.fingerprints["simhash"].value == "0000000000000001"
        assert cached.from_cache
        assert store.get(
            record.path,
            10,
            21,
            "text",
            require_fingerprint=True,
            expected_kinds=("simhash",),
            fp_sig="sig",
        ) is None
