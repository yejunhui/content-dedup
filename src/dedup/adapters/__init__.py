"""Adapter registry and extension dispatch."""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from .archive import ArchiveAdapter
from .audio import AudioAdapter
from .base import ContentAdapter
from .binary import BinaryAdapter
from .document import DocumentAdapter
from .image import ImageAdapter
from .text import TextAdapter
from .video import VideoAdapter

ADAPTER_CLASSES: tuple[type[ContentAdapter], ...] = (
    ImageAdapter,
    VideoAdapter,
    AudioAdapter,
    DocumentAdapter,
    TextAdapter,
    ArchiveAdapter,
    BinaryAdapter,
)


class AdapterRegistry:
    def __init__(self, config: AppConfig) -> None:
        self._adapters: list[ContentAdapter] = [cls(config) for cls in ADAPTER_CLASSES]
        self._by_name = {adapter.name: adapter for adapter in self._adapters}
        self.binary = self._by_name["binary"]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)

    def get(self, name: str) -> ContentAdapter:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"unknown adapter type: {name}") from exc

    def resolve(self, path: Path) -> ContentAdapter:
        name = path.name.lower()
        for adapter in self._adapters:
            if adapter is self.binary:
                continue
            for extension in adapter.extensions:
                if name.endswith(extension):
                    return adapter
            if path.name in adapter.filenames:
                return adapter
        return self.binary
