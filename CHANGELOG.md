# Changelog

All notable changes to this project are documented here.

## [0.2.0] - 2026-11-22

### Added

- Local web console with folder picker, type selection, progress, cancellation,
  thumbnails, per-group keeper selection, and cleanup confirmation.
- Per-selection cleanup API: keep a different file per group and move only the
  selected duplicates.
- Dockerfile and `docker-compose.yml` with ffmpeg and Chromaprint support.
- GitHub Actions CI, MIT license, contribution guide, security policy, issue and
  PR templates.
- Demo data generator and UI screenshots.
- Chinese and English READMEs.

### Changed

- Cleanup now verifies size, mtime, and SHA-256 before every move.
- Exact hashes can be reused when a later scan adds fingerprint layers.
- Progress callbacks and cancellation are supported in the scanning pipeline.

### Detection

- Exact SHA-256 for all supported content types.
- Image pHash/dHash with an RGB histogram gate.
- Video frame pHash sequences with DTW, duration, and color gates.
- Optional Chromaprint audio fingerprints.
- Document/text SimHash.
- Archive manifest hashes.
- SQLite incremental cache with algorithm and tool-availability signatures.

### Known limitations

- Web UI is in Simplified Chinese.
- No login/authentication in the web console; bind to localhost or trusted LAN.
- Semantic embedding layer is reserved but not enabled.
- One-click trash restore is not implemented.
- Legacy binary Office files and image-only PDFs are exact-only.

## [0.1.0] - 2026-11-22

- Initial CLI implementation: layered exact and perceptual deduplication,
  SQLite cache, safe trash cleanup, and Markdown/JSON reports.
