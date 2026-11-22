"""Local HTTP server and JSON API for the ContentDedup web UI."""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..config import AppConfig
from .jobs import JobError, JobManager
from .thumbnails import Thumbnailer, ThumbnailError

logger = logging.getLogger("dedup.web.server")

STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

TYPE_INFO = [
    {
        "name": "image",
        "label": "图片",
        "icon": "🖼️",
        "description": "照片、截图、设计图",
        "default": True,
    },
    {
        "name": "video",
        "label": "视频",
        "icon": "🎬",
        "description": "电影、短片、录屏",
        "default": True,
    },
    {
        "name": "audio",
        "label": "音频",
        "icon": "🎵",
        "description": "音乐、录音、播客",
        "default": True,
    },
    {
        "name": "document",
        "label": "文档",
        "icon": "📄",
        "description": "PDF、Office、电子书",
        "default": True,
    },
    {
        "name": "text",
        "label": "文本",
        "icon": "📝",
        "description": "文本、代码、Markdown",
        "default": True,
    },
    {
        "name": "archive",
        "label": "压缩包",
        "icon": "🗜️",
        "description": "ZIP、TAR、GZ 等",
        "default": True,
    },
    {
        "name": "binary",
        "label": "其他文件",
        "icon": "📦",
        "description": "未知类型，仅精确去重",
        "default": False,
    },
]

MODE_INFO = [
    {
        "value": "all",
        "label": "标准模式",
        "description": "找出完全重复和内容相似的文件（推荐）",
    },
    {
        "value": "exact",
        "label": "仅完全重复",
        "description": "速度最快，只比较 SHA-256",
    },
]


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: WebServer


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ContentDedup/{__version__}"

    @property
    def app(self) -> WebServer:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    # ------------------------------------------------------------- HTTP verbs
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path in {"/", "/index.html"}:
                self._send_static("index.html")
                return
            if method == "GET" and path.startswith("/static/"):
                name = path[len("/static/") :]
                if "/" in name or name not in STATIC_FILES:
                    self._not_found("static file not found")
                    return
                self._send_static(name)
                return
            if path == "/api/health":
                self._send_json(200, {"status": "ok", "version": __version__})
                return
            if path == "/api/config" and method == "GET":
                self._send_json(200, self._config_payload())
                return
            if path == "/api/browse" and method == "GET":
                value = _first(query.get("path"))
                hidden = _first(query.get("hidden")) in {"1", "true", "yes"}
                self._send_json(200, self.app.manager.browse(value, show_hidden=hidden))
                return
            if path == "/api/reports" and method == "GET":
                self._send_json(200, {"reports": self.app.manager.list_reports()})
                return
            if path.startswith("/api/reports/") and method == "GET":
                report_id = path[len("/api/reports/") :]
                self._send_json(200, self.app.manager.get_report(report_id))
                return
            if path == "/api/scan" and method == "POST":
                payload = self._read_json()
                self._send_json(202, self.app.manager.start_scan(payload))
                return
            if path == "/api/apply" and method == "POST":
                payload = self._read_json()
                self._send_json(202, self.app.manager.start_apply(payload))
                return
            if path.startswith("/api/jobs/") and method == "GET":
                job_id = path[len("/api/jobs/") :]
                self._send_json(200, self.app.manager.get_job(job_id))
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel") and method == "POST":
                job_id = path[len("/api/jobs/") : -len("/cancel")]
                self._send_json(200, self.app.manager.cancel_job(job_id))
                return
            if path == "/api/cache" and method == "GET":
                self._send_json(200, self.app.manager.cache_stats())
                return
            if path == "/api/cache/prune" and method == "POST":
                self._send_json(200, self.app.manager.cache_prune())
                return
            if path == "/api/thumb" and method == "GET":
                value = _first(query.get("path"))
                if not value:
                    raise ValueError("missing path")
                try:
                    size = int(_first(query.get("size")) or "160")
                except ValueError as exc:
                    raise ValueError("size must be an integer") from exc
                data, content_type = self.app.thumbs.get(value, size)
                self._send_bytes(200, data, content_type, cache_control="private, max-age=3600")
                return
            self._not_found("not found")
        except KeyError as exc:
            self._send_json(404, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except JobError as exc:
            self._send_json(409, {"error": str(exc)})
        except (ValueError, TypeError, ThumbnailError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("request failed")
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # --------------------------------------------------------------- helpers
    def _config_payload(self) -> dict[str, Any]:
        config = self.app.config
        return {
            "version": __version__,
            "types": TYPE_INFO,
            "modes": MODE_INFO,
            "allow_root": str(self.app.manager.allow_root) if self.app.manager.allow_root else None,
            "defaults": {
                "mode": "all",
                "types": [item["name"] for item in TYPE_INFO if item["default"]],
                "workers": config.scan.workers,
                "min_size": config.scan.min_size,
            },
            "safety": {
                "keep": config.safety.keep,
                "trash_dir": config.safety.trash_dir,
            },
        }

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length > 2_000_000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise TypeError("JSON body must be an object")
        return payload

    def _send_static(self, name: str) -> None:
        content_type = STATIC_FILES[name]
        try:
            data = files("dedup.web").joinpath("static").joinpath(name).read_bytes()
        except (FileNotFoundError, ModuleNotFoundError):
            self._not_found("static file not found")
            return
        self._send_bytes(200, data, content_type, cache_control="no-cache")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", cache_control="no-store")

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, message: str) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._send_json(404, {"error": message})
        else:
            self._send_bytes(404, message.encode("utf-8"), "text/plain; charset=utf-8")


class WebServer:
    def __init__(
        self,
        config: AppConfig,
        *,
        db_path: str | Path,
        reports_dir: str | Path,
        thumb_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.manager = JobManager(
            config,
            db_path=db_path,
            reports_dir=reports_dir,
            thumb_dir=thumb_dir,
            allow_root=allow_root,
        )
        self.thumbs = Thumbnailer(self.manager, config, thumb_dir)
        self._httpd = _HTTPServer((host, port), _RequestHandler)
        self._httpd.app = self
        self._thread: threading.Thread | None = None
        self._started = False
        self._ready = threading.Event()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = str(self._httpd.server_address[0])
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        self._started = True
        self._ready.set()
        self._httpd.serve_forever(poll_interval=0.2)

    def start_background(self) -> None:
        self._started = True
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def shutdown(self) -> None:
        if self._started:
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def serve(
    config: AppConfig,
    *,
    db_path: str | Path,
    reports_dir: str | Path,
    thumb_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_root: str | Path | None = None,
    open_browser: bool = False,
) -> None:
    server = WebServer(
        config,
        db_path=db_path,
        reports_dir=reports_dir,
        thumb_dir=thumb_dir,
        host=host,
        port=port,
        allow_root=allow_root,
    )
    print(f"ContentDedup 控制台已启动: {server.url}", flush=True)
    if server.manager.allow_root is not None:
        print(f"允许访问的根目录: {server.manager.allow_root}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("警告: 当前监听非本机地址且没有身份验证，请只在可信局域网使用。", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        server.shutdown()


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None
