"""Small shared utilities: hashing, atomic writes and formatting."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

CHUNK_SIZE = 1024 * 1024
HASH_NAME = "sha256"


def hash_stream(stream: BinaryIO, max_bytes: int | None = None, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """Return (sha256 hex digest, bytes read) for a binary stream.

    Raises ValueError when max_bytes is exceeded instead of producing a
    truncated digest, because truncated fingerprints are not comparable.
    """
    digest = hashlib.new(HASH_NAME)
    total = 0
    while True:
        block = stream.read(chunk_size)
        if not block:
            break
        total += len(block)
        if max_bytes is not None and total > max_bytes:
            raise ValueError(f"stream exceeds limit of {max_bytes} bytes")
        digest.update(block)
    return digest.hexdigest(), total


def sha256_file(path: str | os.PathLike[str], chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.new(HASH_NAME)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_text(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def human_size(num: float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def safe_relpath(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    try:
        rel = os.path.relpath(os.fspath(path), os.fspath(root))
    except ValueError:
        return os.fspath(path)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return os.fspath(path)
    return rel


def ensure_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def unique_path(path: Path) -> Path:
    """Return a path that does not exist yet, appending .1, .2, ... if needed."""
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.name}.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free destination name for {path}")


def format_distance(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def format_score(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
