"""Fallback adapter: exact SHA-256 only."""

from __future__ import annotations

from .base import ContentAdapter


class BinaryAdapter(ContentAdapter):
    name = "binary"
    extensions = frozenset()
    fingerprint_kinds = ()
