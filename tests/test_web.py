from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from dedup.cleanup import apply_selections
from dedup.config import AppConfig
from dedup.web.server import WebServer

from .conftest import make_image, make_near_image, make_text


@pytest.fixture
def web(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    config = AppConfig()
    config.scan.progress_every = 0
    server = WebServer(
        config,
        db_path=tmp_path / "cache.sqlite3",
        reports_dir=tmp_path / "reports",
        thumb_dir=tmp_path / "thumbs",
        host="127.0.0.1",
        port=0,
        allow_root=root,
    )
    server.start_background()
    try:
        yield server, root, tmp_path
    finally:
        server.shutdown()


def _request(server: WebServer, path: str, method: str = "GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        server.url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def _json(server: WebServer, path: str, method: str = "GET", payload=None):
    status, _headers, body = _request(server, path, method, payload)
    return status, json.loads(body.decode("utf-8"))


def _wait_job(server: WebServer, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job = _json(server, f"/api/jobs/{job_id}")
        assert status == 200
        if job["status"] in {"done", "cancelled", "error"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_static_and_config(web) -> None:
    server, _root, _tmp = web
    status, headers, body = _request(server, "/")
    assert status == 200
    assert b"ContentDedup" in body
    assert headers["Content-Type"].startswith("text/html")
    status, _headers, body = _request(server, "/static/app.js")
    assert status == 200 and b"startScan" in body
    status, config = _json(server, "/api/config")
    assert status == 200
    assert {item["name"] for item in config["types"]} >= {"image", "video", "audio"}
    assert config["allow_root"]


def test_browse_and_scan_thumbnail_and_apply(web) -> None:
    server, root, _tmp_path = web
    (root / "sub").mkdir()
    status, browsed = _json(server, "/api/browse?path=" + urllib.parse.quote(str(root)))
    assert status == 200
    assert browsed["current"] == str(root.resolve())
    assert any(item["name"] == "sub" for item in browsed["entries"])

    status, outside = _json(server, "/api/browse?path=" + urllib.parse.quote("/tmp"))
    assert status == 403
    assert "error" in outside

    base = make_image(root / "base.png")
    make_near_image(base, root / "near.jpg")
    make_text(root / "one.txt")
    two = make_text(root / "two.txt")
    two.write_text(two.read_text(encoding="utf-8") + " extra tail", encoding="utf-8")

    status, job = _json(
        server,
        "/api/scan",
        "POST",
        {
            "root": str(root),
            "types": ["image", "text"],
            "mode": "all",
            "workers": 1,
            "min_size": 1,
        },
    )
    assert status == 202
    job = _wait_job(server, job["id"])
    assert job["status"] == "done", job
    report_id = job["result"]["report_id"]

    status, report = _json(server, f"/api/reports/{urllib.parse.quote(report_id)}")
    assert status == 200
    assert report["summary"]["groups"] >= 1
    assert any(group["kind"] == "image:phash" for group in report["groups"])

    image_member = next(
        member
        for group in report["groups"]
        if group["kind"] == "image:phash"
        for member in [group["keeper"], *group["duplicates"]]
    )
    status, headers, body = _request(
        server,
        "/api/thumb?path=" + urllib.parse.quote(image_member["path"], safe="") + "&size=96",
    )
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8")

    text_group = next(group for group in report["groups"] if group["kind"] == "text:simhash")
    old_keeper = text_group["keeper"]["path"]
    new_keeper = text_group["duplicates"][0]["path"]
    status, apply_job = _json(
        server,
        "/api/apply",
        "POST",
        {
            "report_id": report_id,
            "selections": [
                {
                    "group_id": text_group["id"],
                    "keeper": new_keeper,
                    "duplicates": [old_keeper],
                }
            ],
            "dry_run": False,
            "verify": True,
        },
    )
    assert status == 202
    apply_job = _wait_job(server, apply_job["id"])
    assert apply_job["status"] == "done", apply_job
    assert apply_job["result"]["moved"] == 1
    assert apply_job["result"]["errors"] == 0
    assert Path(new_keeper).exists()
    assert not Path(old_keeper).exists()
    assert apply_job["result"]["manifest"]
    assert Path(apply_job["result"]["manifest"]).is_file()

    status, reports = _json(server, "/api/reports")
    assert status == 200
    assert any(item["id"] == report_id for item in reports["reports"])

    status, stats = _json(server, "/api/cache")
    assert status == 200 and stats["files"] == 4


def test_apply_selections_validation(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    make_text(root / "one.txt")
    make_text(root / "two.txt")
    config = AppConfig()
    from dedup.adapters import AdapterRegistry
    from dedup.pipeline import Scanner
    from dedup.report import build_report
    from dedup.store import CacheStore

    with CacheStore(tmp_path / "cache.sqlite3") as store:
        result = Scanner(config, store, AdapterRegistry(config)).scan(root)
    report = build_report(result, config)
    group = report["groups"][0]

    summary = apply_selections(
        report,
        [
            {
                "group_id": group["id"],
                "keeper": group["keeper"]["path"],
                "duplicates": [group["duplicates"][0]["path"]],
            }
        ],
        dry_run=True,
    )
    assert summary["planned"] == 1

    with pytest.raises(ValueError):
        apply_selections(
            report,
            [
                {
                    "group_id": group["id"],
                    "keeper": group["keeper"]["path"],
                    "duplicates": ["/not/in/report"],
                }
            ],
            dry_run=True,
        )
    with pytest.raises(ValueError):
        apply_selections(
            report,
            [
                {
                    "group_id": group["id"],
                    "keeper": group["keeper"]["path"],
                    "duplicates": [group["keeper"]["path"]],
                }
            ],
            dry_run=True,
        )
    with pytest.raises(ValueError):
        apply_selections(
            report,
            [
                {
                    "group_id": group["id"],
                    "keeper": group["keeper"]["path"],
                    "duplicates": [group["duplicates"][0]["path"]],
                },
                {
                    "group_id": group["id"],
                    "keeper": group["keeper"]["path"],
                    "duplicates": [group["duplicates"][0]["path"]],
                },
            ],
            dry_run=True,
        )
