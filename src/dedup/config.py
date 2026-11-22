"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised for invalid or unsupported configuration."""


@dataclass(slots=True)
class ScanConfig:
    exclude: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".svn",
            ".hg",
            "@eaDir",
            "#recycle",
            "node_modules",
            "__pycache__",
            ".dedup",
            ".dedup-trash",
            ".venv",
            "venv",
            "*.tmp",
            "~$*",
        ]
    )
    workers: int = 1
    min_size: int = 1
    follow_symlinks: bool = False
    progress_every: int = 200


@dataclass(slots=True)
class ImageConfig:
    algorithm: str = "phash"
    hash_size: int = 8
    phash_threshold: int = 8
    dhash_threshold: int = 8
    color_threshold: float = 0.85


@dataclass(slots=True)
class VideoConfig:
    frame_interval: float = 5.0
    max_frames: int = 240
    hash_size: int = 8
    phash_threshold: int = 10
    duration_tolerance: float = 0.2
    color_threshold: float = 0.8
    max_pair_comparisons: int = 200_000
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout: int = 600


@dataclass(slots=True)
class AudioConfig:
    enabled: bool = True
    fpcalc: str = "fpcalc"
    length: int = 120
    min_score: float = 0.9
    duration_tolerance: float = 0.15
    max_shift: int = 30
    min_overlap: int = 40
    max_pair_comparisons: int = 200_000
    timeout: int = 60


@dataclass(slots=True)
class DocumentConfig:
    simhash_hamming: int = 3
    min_tokens: int = 20
    max_chars: int = 2_000_000
    max_pages: int = 500


@dataclass(slots=True)
class TextConfig:
    simhash_hamming: int = 3
    min_tokens: int = 20
    max_bytes: int = 4_000_000


@dataclass(slots=True)
class ArchiveConfig:
    max_entries: int = 5000
    max_uncompressed_bytes: int = 2_000_000_000
    max_inner_hash_bytes: int = 2_000_000_000


@dataclass(slots=True)
class SemanticConfig:
    enabled: bool = False
    model: str = ""
    cosine_threshold: float = 0.92


@dataclass(slots=True)
class SafetyConfig:
    keep: str = "oldest"
    trash_dir: str = ".dedup-trash"
    verify_on_apply: bool = True


@dataclass(slots=True)
class StorageConfig:
    database: str = ".dedup/cache.sqlite3"
    report: str = ".dedup/reports/latest.json"


@dataclass(slots=True)
class AppConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    document: DocumentConfig = field(default_factory=DocumentConfig)
    text: TextConfig = field(default_factory=TextConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    semantic: SemanticConfig = field(default_factory=SemanticConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AppConfig:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError("top-level configuration must be a mapping")
        allowed = {f.name for f in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"unknown configuration sections: {', '.join(sorted(unknown))}")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            section_cls = f.type
            if isinstance(section_cls, str):
                section_cls = globals()[section_cls]
            kwargs[f.name] = _load_section(section_cls, data.get(f.name), f.name)
        config = cls(**kwargs)
        config.validate()
        return config

    def validate(self) -> None:
        if not 1 <= self.scan.workers <= 128:
            raise ConfigError("scan.workers must be between 1 and 128")
        if self.scan.min_size < 0:
            raise ConfigError("scan.min_size must be non-negative")
        if self.scan.progress_every < 0:
            raise ConfigError("scan.progress_every must be non-negative")
        if not isinstance(self.scan.exclude, list) or not all(
            isinstance(item, str) for item in self.scan.exclude
        ):
            raise ConfigError("scan.exclude must be a list of glob strings")

        if self.image.algorithm not in {"phash", "dhash"}:
            raise ConfigError("image.algorithm must be 'phash' or 'dhash'")
        if not 4 <= self.image.hash_size <= 16:
            raise ConfigError("image.hash_size must be between 4 and 16")
        _check_hamming("image.phash_threshold", self.image.phash_threshold)
        _check_hamming("image.dhash_threshold", self.image.dhash_threshold)
        _check_unit_interval("image.color_threshold", self.image.color_threshold)

        if self.video.frame_interval <= 0:
            raise ConfigError("video.frame_interval must be positive")
        if self.video.max_frames < 1:
            raise ConfigError("video.max_frames must be at least 1")
        if not 4 <= self.video.hash_size <= 16:
            raise ConfigError("video.hash_size must be between 4 and 16")
        _check_hamming("video.phash_threshold", self.video.phash_threshold)
        _check_unit_interval("video.duration_tolerance", self.video.duration_tolerance)
        _check_unit_interval("video.color_threshold", self.video.color_threshold)
        if self.video.max_pair_comparisons < 1:
            raise ConfigError("video.max_pair_comparisons must be positive")
        if self.video.timeout < 1:
            raise ConfigError("video.timeout must be positive")

        if self.audio.length < 1:
            raise ConfigError("audio.length must be positive")
        _check_unit_interval("audio.min_score", self.audio.min_score)
        _check_unit_interval("audio.duration_tolerance", self.audio.duration_tolerance)
        if self.audio.max_shift < 0 or self.audio.min_overlap < 1:
            raise ConfigError("audio.max_shift must be non-negative and audio.min_overlap positive")
        if self.audio.max_pair_comparisons < 1:
            raise ConfigError("audio.max_pair_comparisons must be positive")
        if self.audio.timeout < 1:
            raise ConfigError("audio.timeout must be positive")

        _check_hamming("document.simhash_hamming", self.document.simhash_hamming)
        _check_hamming("text.simhash_hamming", self.text.simhash_hamming)
        if self.document.min_tokens < 1 or self.text.min_tokens < 1:
            raise ConfigError("document/text min_tokens must be positive")
        if self.document.max_chars < 1 or self.document.max_pages < 1:
            raise ConfigError("document.max_chars and document.max_pages must be positive")
        if self.text.max_bytes < 1:
            raise ConfigError("text.max_bytes must be positive")

        if self.archive.max_entries < 1 or self.archive.max_uncompressed_bytes < 1:
            raise ConfigError("archive limits must be positive")
        if self.archive.max_inner_hash_bytes < 1:
            raise ConfigError("archive.max_inner_hash_bytes must be positive")

        if self.safety.keep not in {"oldest", "newest", "largest", "shortest_path"}:
            raise ConfigError("safety.keep must be oldest, newest, largest or shortest_path")
        if not self.safety.trash_dir:
            raise ConfigError("safety.trash_dir must not be empty")
        if not self.storage.database or not self.storage.report:
            raise ConfigError("storage.database and storage.report must not be empty")


def _load_section(section_cls: type, data: Any, name: str) -> Any:
    if data is None:
        return section_cls()
    if not isinstance(data, dict):
        raise ConfigError(f"configuration section '{name}' must be a mapping")
    allowed = {f.name for f in fields(section_cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in '{name}': {', '.join(sorted(unknown))}")
    try:
        return section_cls(**data)
    except TypeError as exc:
        raise ConfigError(f"invalid configuration in '{name}': {exc}") from exc


def _check_hamming(name: str, value: int) -> None:
    if not isinstance(value, int) or not 0 <= value <= 64:
        raise ConfigError(f"{name} must be an integer between 0 and 64")


def _check_unit_interval(name: str, value: float) -> None:
    if not 0.0 <= float(value) <= 1.0:
        raise ConfigError(f"{name} must be between 0 and 1")


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    return AppConfig.from_dict(raw)
