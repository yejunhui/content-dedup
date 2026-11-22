"""Plain text, source code and markup adapter: SHA-256 plus SimHash."""

from __future__ import annotations

from pathlib import Path

from ..fingerprints import SIMHASH_VERSION
from ..models import Fingerprint
from .base import ContentAdapter, MatchInfo, hamming_match
from .textutil import (
    build_simhash_fingerprint,
    html_to_text,
    read_text_file,
    simhash_sig,
)

_MARKUP_SUFFIXES = {".html", ".htm", ".xhtml", ".xml"}


class TextAdapter(ContentAdapter):
    name = "text"
    extensions = frozenset(
        {
            ".txt",
            ".text",
            ".md",
            ".markdown",
            ".rst",
            ".log",
            ".csv",
            ".tsv",
            ".json",
            ".jsonl",
            ".ndjson",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".xml",
            ".html",
            ".htm",
            ".xhtml",
            ".css",
            ".scss",
            ".less",
            ".js",
            ".mjs",
            ".cjs",
            ".jsx",
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
            ".vue",
            ".svelte",
            ".py",
            ".pyi",
            ".ipynb",
            ".java",
            ".kt",
            ".kts",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".cxx",
            ".hpp",
            ".cs",
            ".go",
            ".rs",
            ".rb",
            ".php",
            ".pl",
            ".pm",
            ".lua",
            ".r",
            ".swift",
            ".scala",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".ps1",
            ".bat",
            ".cmd",
            ".sql",
            ".tex",
            ".bib",
            ".proto",
            ".graphql",
            ".gql",
            ".dockerfile",
            ".gitignore",
            ".gitattributes",
            ".editorconfig",
        }
    )
    filenames = frozenset(
        {
            "Dockerfile",
            "Containerfile",
            "Makefile",
            "GNUmakefile",
            "CMakeLists.txt",
            "LICENSE",
            "LICENCE",
            "COPYING",
            "README",
            "CHANGELOG",
            "AUTHORS",
            "NOTICE",
        }
    )
    fingerprint_kinds = ("simhash",)

    def fingerprint_sig(self) -> str:
        return simhash_sig(
            {
                "version": 1,
                "min_tokens": self.config.text.min_tokens,
                "max_bytes": self.config.text.max_bytes,
                "tokenizer": 1,
                "simhash_version": SIMHASH_VERSION,
            }
        )

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        text, encoding, truncated = read_text_file(path, self.config.text.max_bytes)
        if path.suffix.lower() in _MARKUP_SUFFIXES:
            text = html_to_text(text)
        fingerprint = build_simhash_fingerprint(
            text,
            min_tokens=self.config.text.min_tokens,
            extra_meta={"encoding": encoding, "truncated": truncated},
        )
        return {fingerprint.kind: fingerprint}

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != "simhash":
            return None
        return hamming_match(left, right, self.config.text.simhash_hamming)
