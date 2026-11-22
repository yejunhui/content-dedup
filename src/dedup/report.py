"""Report construction and Markdown rendering."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig
from .models import ScanResult
from .util import (
    atomic_write_json,
    atomic_write_text,
    format_distance,
    format_score,
    human_size,
)

DEFAULT_MARKDOWN_GROUPS = 200
MAX_SCAN_WARNINGS = 200


def build_report(result: ScanResult, config: AppConfig) -> dict[str, Any]:
    groups = [group.to_dict() for group in result.groups]
    duplicate_count = sum(len(group["duplicates"]) for group in groups)
    reclaimable = sum(group["reclaimable_bytes"] for group in groups)
    exact_groups = sum(1 for group in groups if group["kind"] == "exact")

    issue_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    issue_examples: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for record in result.records:
        if not record.fp_error:
            continue
        key = (record.adapter, record.fp_status, record.fp_error)
        issue_counts[key] += 1
        if len(issue_examples[key]) < 5:
            issue_examples[key].append(record.path)

    issues = [
        {
            "adapter": adapter,
            "status": status,
            "message": message,
            "count": count,
            "examples": issue_examples[(adapter, status, message)],
        }
        for (adapter, status, message), count in sorted(
            issue_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    report = {
        "tool": "content-dedup",
        "version": __version__,
        "generated_at": result.generated_at,
        "root": result.root,
        "mode": result.mode,
        "layers": result.layers,
        "stats": result.stats.to_dict(),
        "summary": {
            "groups": len(groups),
            "exact_groups": exact_groups,
            "fingerprint_groups": len(groups) - exact_groups,
            "duplicate_files": duplicate_count,
            "reclaimable_bytes": reclaimable,
            "reclaimable_human": human_size(reclaimable),
        },
        "settings": config.to_dict(),
        "groups": groups,
        "fingerprint_issues": issues,
        "scan_warnings": {
            "total": len(result.warnings),
            "items": result.warnings[:MAX_SCAN_WARNINGS],
        },
    }
    return report


def write_report(
    result: ScanResult,
    config: AppConfig,
    path: str | Path,
    *,
    markdown_groups: int = DEFAULT_MARKDOWN_GROUPS,
) -> tuple[Path, Path]:
    report = build_report(result, config)
    target = Path(path).expanduser()
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    json_path = target
    md_path = target.with_suffix(".md")
    atomic_write_json(json_path, report)
    atomic_write_text(md_path, render_markdown(report, top=markdown_groups))
    return json_path, md_path


def load_report(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"report is not a JSON object: {path}")
    return data


def render_markdown(report: dict[str, Any], *, top: int = DEFAULT_MARKDOWN_GROUPS) -> str:
    lines: list[str] = []
    stats = report.get("stats", {})
    summary = report.get("summary", {})
    lines.append("# ContentDedup report")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- Version: `{report.get('version', '')}`")
    lines.append(f"- Root: {_md_path(report.get('root', ''))}")
    lines.append(f"- Mode: `{report.get('mode', '')}`  Layers: {', '.join(report.get('layers', []))}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    rows = [
        ("Files seen", stats.get("files_seen", 0)),
        ("Files processed", stats.get("files_processed", 0)),
        ("Cache hits", stats.get("cache_hits", 0)),
        ("Exact hashes reused", stats.get("exact_reused", 0)),
        ("Duplicate groups", summary.get("groups", 0)),
        ("Exact groups", summary.get("exact_groups", 0)),
        ("Fingerprint groups", summary.get("fingerprint_groups", 0)),
        ("Duplicate files", summary.get("duplicate_files", 0)),
        (
            "Reclaimable",
            (
                f"{summary.get('reclaimable_human', human_size(summary.get('reclaimable_bytes', 0)))} "
                f"({summary.get('reclaimable_bytes', 0)} bytes)"
            ),
        ),
        ("Fingerprints OK", stats.get("fingerprints_ok", 0)),
        ("Fingerprints unsupported", stats.get("fingerprints_unsupported", 0)),
        ("Fingerprints failed", stats.get("fingerprints_error", 0)),
        ("Elapsed", f"{stats.get('elapsed_seconds', 0)} s"),
    ]
    for label, value in rows:
        lines.append(f"| {label} | {value} |")
    lines.append("")

    issues = report.get("fingerprint_issues") or []
    if issues:
        lines.append("## Fingerprint issues")
        lines.append("")
        lines.append("| Adapter | Status | Count | Message | Example |")
        lines.append("|---|---|---:|---|---|")
        for issue in issues:
            examples = issue.get("examples") or []
            example = _md_path(examples[0]) if examples else ""
            message = str(issue.get("message", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {issue.get('adapter', '')} | {issue.get('status', '')} | "
                f"{issue.get('count', 0)} | {message} | {example} |"
            )
        lines.append("")

    warnings = report.get("scan_warnings") or {}
    if warnings.get("total"):
        lines.append("## Scan warnings")
        lines.append("")
        for item in warnings.get("items", []):
            lines.append(f"- {_md_path(item)}")
        remaining = int(warnings.get("total", 0)) - len(warnings.get("items", []))
        if remaining > 0:
            lines.append(f"- ... and {remaining} more warning(s)")
        lines.append("")

    groups = report.get("groups") or []
    lines.append(f"## Duplicate groups ({len(groups)})")
    lines.append("")
    if not groups:
        lines.append("No duplicates found.")
        lines.append("")
        return "\n".join(lines)

    for index, group in enumerate(groups[:top], start=1):
        keeper = group["keeper"]
        duplicates = group["duplicates"]
        reason = group.get("kind", "")
        lines.append(
            f"### {index}. {reason} — {len(duplicates) + 1} files, "
            f"{human_size(group.get('reclaimable_bytes', 0))} reclaimable"
        )
        lines.append("")
        lines.append(f"Keep: {_md_path(keeper['rel_path'])}")
        lines.append("")
        for duplicate in duplicates:
            lines.append(f"- {_md_path(duplicate['rel_path'])} — {_describe_match(duplicate)}")
        lines.append("")

    if len(groups) > top:
        lines.append(f"_... and {len(groups) - top} more group(s) in the JSON report._")
        lines.append("")
    return "\n".join(lines)


def _describe_match(duplicate: dict[str, Any]) -> str:
    reason = duplicate.get("reason", "")
    distance = duplicate.get("distance")
    score = duplicate.get("score")
    if reason == "exact":
        return "exact duplicate (same SHA-256)"
    if reason in {"phash", "dhash"}:
        return f"{reason} distance {format_distance(distance)}"
    if reason == "simhash":
        return f"SimHash distance {format_distance(distance)}"
    if reason == "phash_seq":
        return f"video frame pHash distance {format_distance(distance)}"
    if reason == "chromaprint":
        return f"audio fingerprint score {format_score(score)}"
    if reason == "manifest":
        return "archive manifest match"
    if distance is not None:
        return f"{reason} distance {format_distance(distance)}"
    if score is not None:
        return f"{reason} score {format_score(score)}"
    return reason or "similar"


def _md_path(value: str) -> str:
    if not value:
        return ""
    cleaned = str(value).replace("`", "'").replace("\n", " ")
    return f"`{cleaned}`"
