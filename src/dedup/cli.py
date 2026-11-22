"""Command line interface for ContentDedup."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .adapters import AdapterRegistry
from .cleanup import apply_report
from .config import AppConfig, ConfigError, load_config
from .pipeline import Scanner
from .report import DEFAULT_MARKDOWN_GROUPS, load_report, render_markdown, write_report
from .store import CacheStore
from .util import atomic_write_json, human_size

logger = logging.getLogger("dedup.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup",
        description="Layered multi-modal exact and perceptual content deduplication.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan a directory and write a duplicate report")
    scan.add_argument("root", help="directory to scan")
    _add_common(scan)
    scan.add_argument("--config", help="YAML configuration file (default: ./config.yaml if present)")
    scan.add_argument(
        "--types",
        help="comma-separated content types: image,video,audio,document,text,archive,binary",
    )
    scan.add_argument(
        "--mode",
        choices=("exact", "fingerprint", "all"),
        default="all",
        help="exact layer only, stop before semantic, or all configured layers (default: all)",
    )
    scan.add_argument("--db", help="SQLite cache path")
    scan.add_argument("--report", help="report path (JSON; a .md file is written next to it)")
    scan.add_argument("--limit", type=int, help="process at most N files (after sorting)")
    scan.add_argument("--workers", type=int, help="parallel hashing/fingerprint workers")
    scan.add_argument("--min-size", type=int, help="skip files smaller than N bytes")
    scan.add_argument("--follow-symlinks", action="store_true", help="follow symlinks")
    scan.add_argument("--refresh", action="store_true", help="ignore the incremental cache")
    scan.add_argument("--no-semantic", action="store_true", help="disable the semantic layer")

    report = subparsers.add_parser("report", help="render a saved JSON report")
    _add_common(report)
    report.add_argument("--report", required=True, help="JSON report path")
    report.add_argument("--top", type=int, default=DEFAULT_MARKDOWN_GROUPS, help="groups to show")
    report.add_argument("--out", help="write Markdown to this path instead of stdout")

    apply = subparsers.add_parser("apply", help="move duplicates from a report into trash")
    _add_common(apply)
    apply.add_argument("--report", required=True, help="JSON report path")
    apply.add_argument("--trash", help="trash directory (default: <root>/.dedup-trash/<timestamp>)")
    apply.add_argument("--yes", action="store_true", help="actually move files (default: dry run)")
    apply.add_argument("--no-verify", action="store_true", help="skip content hash verification")
    apply.add_argument("--output", help="write the apply summary as JSON")

    cache = subparsers.add_parser("cache", help="inspect or prune the incremental cache")
    _add_common(cache)
    cache.add_argument("--config", help="YAML configuration file")
    cache.add_argument("--db", help="SQLite cache path")
    cache.add_argument("action", choices=("stats", "prune", "clear"))
    cache.add_argument("--yes", action="store_true", help="confirm cache clear")

    web = subparsers.add_parser("web", help="start the local web interface")
    _add_common(web)
    web.add_argument("--config", help="YAML configuration file")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    web.add_argument("--root", help="restrict browsing and scanning to this directory")
    web.add_argument("--db", help="SQLite cache path")
    web.add_argument("--reports", help="directory for web scan reports")
    web.add_argument("--thumbs", help="directory for thumbnail cache")
    web.add_argument("--open", action="store_true", help="open the browser automatically")
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "report":
            return _report(args)
        if args.command == "apply":
            return _apply(args)
        if args.command == "cache":
            return _cache(args)
        if args.command == "web":
            return _web(args)
    except (ConfigError, ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    parser.print_help()
    return 2


def _setup_logging(args: argparse.Namespace) -> None:
    if getattr(args, "quiet", False):
        level = logging.WARNING
    elif getattr(args, "verbose", 0) > 0:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)


def _load_config(args: argparse.Namespace) -> AppConfig:
    config_path = getattr(args, "config", None)
    if config_path:
        return load_config(config_path)
    default_path = Path("config.yaml")
    if default_path.is_file():
        return load_config(default_path)
    return AppConfig()


def _scan(args: argparse.Namespace) -> int:
    config = _load_config(args)
    if args.no_semantic:
        config.semantic.enabled = False
    if config.semantic.enabled:
        raise ConfigError(
            "the semantic layer is not implemented in this version; use --no-semantic"
        )
    if args.workers is not None:
        config.scan.workers = args.workers
    if args.min_size is not None:
        config.scan.min_size = args.min_size
    if args.follow_symlinks:
        config.scan.follow_symlinks = True
    config.validate()
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be non-negative")

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"scan root is not a directory: {root}")

    types = _parse_types(args.types)
    registry = AdapterRegistry(config)
    if types is not None:
        unknown = types - set(registry.names)
        if unknown:
            raise ValueError(
                f"unknown types: {', '.join(sorted(unknown))}; available: {', '.join(registry.names)}"
            )

    db_path = _resolve_storage_path(args.db or config.storage.database)
    report_path = _resolve_storage_path(args.report or config.storage.report)
    report_json = report_path if report_path.suffix.lower() == ".json" else report_path.with_suffix(".json")
    report_md = report_json.with_suffix(".md")
    trash_config = Path(config.safety.trash_dir).expanduser()
    trash_root = trash_config if trash_config.is_absolute() else root / trash_config

    exclude_paths: list[str | Path] = [report_json, report_md, trash_root]
    if str(db_path) != ":memory:":
        exclude_paths.append(db_path)

    with CacheStore(db_path) as store:
        scanner = Scanner(config, store, registry, exclude_paths=exclude_paths)
        result = scanner.scan(
            root,
            mode=args.mode,
            types=types,
            limit=args.limit,
            refresh=args.refresh,
        )

    json_path, md_path = write_report(result, config, report_json)
    summary = result.stats
    print(
        f"Processed {summary.files_processed} file(s): {len(result.groups)} duplicate group(s), "
        f"{summary.cache_hits} cache hit(s), {summary.exact_reused} exact hash reuse(s), "
        f"{summary.errors} error(s)."
    )
    reclaimable = sum(group.reclaimable_bytes for group in result.groups)
    print(
        f"Duplicate files: {sum(len(group.duplicates) for group in result.groups)}; "
        f"reclaimable: {human_size(reclaimable)}."
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    return 0


def _report(args: argparse.Namespace) -> int:
    report = load_report(args.report)
    markdown = render_markdown(report, top=max(0, args.top))
    if args.out:
        from .util import atomic_write_text

        atomic_write_text(args.out, markdown)
        print(f"Markdown report: {args.out}")
    else:
        print(markdown)
    return 0


def _apply(args: argparse.Namespace) -> int:
    report = load_report(args.report)
    dry_run = not args.yes
    summary = apply_report(
        report,
        trash_dir=args.trash,
        dry_run=dry_run,
        verify=not args.no_verify and not dry_run,
    )
    if args.output:
        atomic_write_json(args.output, summary)
        print(f"Summary: {args.output}")
    print(
        f"{'Would move' if dry_run else 'Moved'} {summary['moved'] + summary['planned']} file(s); "
        f"skipped {summary['skipped']}, errors {summary['errors']}; "
        f"reclaimable {human_size(summary['reclaimable_bytes'])}."
    )
    if dry_run:
        print("Dry run only. Re-run with --yes to move files into trash.")
    else:
        print(f"Trash: {summary['trash']}")
        if summary.get("manifest"):
            print(f"Manifest: {summary['manifest']}")
    for item in summary["items"][:20]:
        print(f"  {item['source']} -> {item['destination']}")
    if len(summary["items"]) > 20:
        print(f"  ... and {len(summary['items']) - 20} more")
    for warning in summary["warnings"][:20]:
        print(f"  skip: {warning['path']}: {warning['reason']}", file=sys.stderr)
    return 1 if summary["errors"] else 0


def _cache(args: argparse.Namespace) -> int:
    config = _load_config(args)
    db_path = _resolve_storage_path(args.db or config.storage.database)
    with CacheStore(db_path) as store:
        if args.action == "stats":
            print(json.dumps(store.stats(), indent=2))
            return 0
        if args.action == "prune":
            removed = store.prune_missing()
            print(f"Removed {removed} stale cache entr{'y' if removed == 1 else 'ies'}.")
            return 0
        if args.action == "clear":
            if not args.yes:
                print("Refusing to clear the cache without --yes.", file=sys.stderr)
                return 2
            removed = store.clear()
            print(f"Cleared {removed} cache entries.")
            return 0
    return 2


def _web(args: argparse.Namespace) -> int:
    from .web.server import serve

    config = _load_config(args)
    if not 0 <= args.port <= 65535:
        raise ValueError("--port must be between 0 and 65535")
    db_path = _resolve_storage_path(args.db or config.storage.database)
    report_config = Path(config.storage.report).expanduser()
    reports_dir = Path(args.reports).expanduser() if args.reports else report_config.parent
    thumbs_dir = (
        Path(args.thumbs).expanduser()
        if args.thumbs
        else Path(".dedup/thumbnails")
    )
    serve(
        config,
        db_path=db_path,
        reports_dir=reports_dir.resolve(),
        thumb_dir=thumbs_dir.resolve(),
        host=args.host,
        port=args.port,
        allow_root=args.root,
        open_browser=args.open,
    )
    return 0


def _parse_types(value: str | None) -> set[str] | None:
    if value is None:
        return None
    types = {part.strip().lower() for part in value.split(",") if part.strip()}
    if not types:
        raise ValueError("--types must not be empty")
    return types


def _resolve_storage_path(value: str | Path) -> Path:
    if str(value) == ":memory:":
        return Path(":memory:")
    return Path(value).expanduser().resolve()


if __name__ == "__main__":
    raise SystemExit(main())
