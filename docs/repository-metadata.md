# Repository metadata

Use this checklist after pushing the repository so people can actually find it.

## GitHub

**Description**

```text
Local, safe multi-modal content deduplication for images, video, audio, documents, text and archives. Web UI + CLI. Preview first, move to trash, never delete.
```

**Topics** (Settings → Topics)

```text
content-dedup
duplicate-files
duplicate-photos
image-deduplication
video-deduplication
audio-fingerprint
perceptual-hash
phash
simhash
deduplication
nas
synology
self-hosted
privacy
python
web-ui
file-management
docker
open-source
selfhosted
```

**Social preview**

Upload [`docs/social-preview.png`](social-preview.png) in **Settings → Social preview**.
Recommended size: 1280×640 (already prepared).

**About website**

Use the GitHub repository URL, or leave the website blank until a docs site exists.

## Gitee

**仓库介绍**

```text
本地安全的多模态内容去重：图片 / 视频 / 音频 / 文档 / 压缩包。Web 图形界面 + CLI，先预览后移入回收站，不永久删除。支持增量扫描和 Docker/NAS。
```

**标签 / 关键词**

```text
内容去重 图片去重 视频去重 音频指纹 SimHash pHash 重复文件 文件管理 NAS 群晖 自托管 隐私 开源 Python Docker
```

**开源许可证**

Select **MIT** and point to [`LICENSE`](../LICENSE).

## Release notes for v0.2.0

```markdown
## ContentDedup v0.2.0

First public release with a local web interface.

### Added
- Local web console: directory picker, type selection, progress, cancel, thumbnails
- Per-group keeper selection and per-file cleanup confirmation
- Safe trash workflow with manifest/journal and re-verification
- Docker image and docker-compose example
- GitHub Actions CI, MIT license, issue/PR templates
- Demo generator and UI screenshots

### Detection
- Exact SHA-256 for all content types
- Image pHash/dHash + color histogram
- Video frame pHash sequence + DTW + color gate
- Optional Chromaprint audio fingerprints
- Document/text SimHash
- Archive manifest hash

### Notes
- Web UI is currently in Simplified Chinese
- Semantic embedding layer is not enabled yet
- The web console has no authentication; bind to localhost or a trusted LAN/VPN
```
