"""Thumbnail generation and on-disk caching for the web UI."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from ..adapters import AdapterRegistry
from ..config import AppConfig
from .jobs import JobManager

logger = logging.getLogger("dedup.web.thumbs")

THUMB_VERSION = 1


class ThumbnailError(RuntimeError):
    pass


class Thumbnailer:
    def __init__(self, manager: JobManager, config: AppConfig, thumb_dir: str | Path) -> None:
        self.manager = manager
        self.config = config
        self.thumb_dir = Path(thumb_dir).expanduser().resolve()
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.registry = AdapterRegistry(config)

    def get(self, path_value: str, size: int = 160) -> tuple[bytes, str]:
        size = max(64, min(512, int(size)))
        path = self.manager.is_thumbnail_path_allowed(path_value)
        if path is None:
            raise PermissionError("file is not part of a loaded report")
        adapter = self.registry.resolve(path)
        if adapter.name not in {"image", "video"}:
            raise ThumbnailError("no thumbnail for this file type")
        try:
            info = path.stat()
        except OSError as exc:
            raise FileNotFoundError(str(exc)) from exc
        key = hashlib.sha256(
            f"{THUMB_VERSION}|{path}|{info.st_mtime_ns}|{size}".encode()
        ).hexdigest()
        cache_path = self.thumb_dir / f"{key}.jpg"
        if cache_path.is_file():
            try:
                return cache_path.read_bytes(), "image/jpeg"
            except OSError:
                pass
        if adapter.name == "image":
            data = self._image_thumb(path, size)
        else:
            data = self._video_thumb(path, size)
        self._write_cache(cache_path, data)
        return data, "image/jpeg"

    def _image_thumb(self, path: Path, size: int) -> bytes:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                if (
                    image.mode in {"RGBA", "LA"}
                    or (image.mode == "P" and "transparency" in image.info)
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    image = Image.alpha_composite(background, rgba)
                image = image.convert("RGB")
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=82, optimize=True)
                return buffer.getvalue()
        except Exception as exc:
            raise ThumbnailError(f"cannot create image thumbnail: {exc}") from exc

    def _video_thumb(self, path: Path, size: int) -> bytes:
        ffmpeg = shutil.which(self.config.video.ffmpeg)
        if not ffmpeg:
            raise ThumbnailError("ffmpeg is not available")
        scale = f"scale='min({size},iw)':-2:force_original_aspect_ratio=decrease"
        last_error = ""
        for seek in ("1", "0"):
            command = [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-ss",
                seek,
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-vf",
                scale,
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last_error = "ffmpeg thumbnail timed out"
                continue
            if result.returncode == 0 and result.stdout:
                return result.stdout
            last_error = result.stderr.decode("utf-8", errors="replace").strip()
        raise ThumbnailError(f"cannot create video thumbnail: {last_error or 'unknown error'}")

    def _write_cache(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".thumb-", suffix=".jpg", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
