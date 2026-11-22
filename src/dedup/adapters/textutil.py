"""Shared text extraction helpers for the text and document adapters."""

from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..fingerprints import SIMHASH_VERSION, simhash64_from_tokens, text_tokens, to_hex64
from ..models import Fingerprint
from .base import UnsupportedFingerprint

TOKENIZER_VERSION = SIMHASH_VERSION


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(markup: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 - fall back to a regex for malformed markup
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def build_simhash_fingerprint(
    text: str,
    *,
    min_tokens: int,
    extra_meta: dict[str, Any] | None = None,
) -> Fingerprint:
    tokens = text_tokens(text)
    if len(tokens) < min_tokens:
        raise UnsupportedFingerprint(f"not enough text tokens ({len(tokens)} < {min_tokens})")
    value, token_count = simhash64_from_tokens(tokens)
    meta: dict[str, Any] = {
        "tokens": token_count,
        "chars": len(text),
        "tokenizer": TOKENIZER_VERSION,
    }
    if extra_meta:
        meta.update(extra_meta)
    return Fingerprint(kind="simhash", version=SIMHASH_VERSION, value=to_hex64(value), meta=meta)


def simhash_sig(settings: dict[str, Any]) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class TextBudget:
    """Collect text chunks while enforcing a hard character budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def add(self, value: Any) -> bool:
        if value is None:
            return True
        if self.length >= self.limit:
            self.truncated = True
            return False
        chunk = str(value)
        remaining = self.limit - self.length
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
            self.truncated = True
        self.parts.append(chunk)
        self.length += len(chunk)
        return not self.truncated

    def text(self) -> str:
        return "".join(self.parts)


def read_text_file(path: Path, max_bytes: int) -> tuple[str, str, bool]:
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        raise UnsupportedFingerprint(
            f"text file exceeds max_bytes ({max_bytes}); exact hash only"
        )
    if b"\x00" in data[:8192]:
        raise UnsupportedFingerprint("file looks binary; exact hash only")
    text, encoding = decode_text(data)
    return text, encoding, truncated
