# ContentDedup

多模态内容去重与相似识别工具。实现 `project_readme.md` 中定义的 **Adapter 抽象 + 分层流水线 + 按类型建索引**，当前交付前两层：

1. **精确哈希**：所有类型统一的 SHA-256。
2. **感知/结构指纹**：图片 pHash/dHash、视频抽帧 pHash 序列、音频 Chromaprint、文档/文本 SimHash、压缩包内部清单哈希。

语义向量层（CLIP/文本 embedding）只预留配置与接口，本版本未实现。

代码托管：[GitHub](https://github.com/yejunhui/content-dedup) · [Gitee](https://gitee.com/PharaohMan/content-dedup)

## 能力矩阵

| 类型 | 精确 | 结构指纹 | 说明 |
|---|---|---|---|
| 图片 | ✅ | pHash 或 dHash + 颜色直方图校验 | 识别缩放、重压缩；颜色差异过大时拒绝合并 |
| 视频 | ✅ | 定时抽帧 pHash 序列 + DTW + 颜色直方图 | 识别重编码、分辨率变化；时长差或整体色调差过大不比较 |
| 音频 | ✅ | Chromaprint（`fpcalc`） | 缺少 `fpcalc` 时自动降级为仅精确去重 |
| PDF / Office / ODF / EPUB | ✅ | 文本提取 + SimHash | 旧版 `.doc/.xls/.ppt` 仅精确去重 |
| 纯文本 / 代码 / Markdown | ✅ | SimHash + shingles | 文本过短（默认 <20 tokens）仅精确去重 |
| zip / tar / gzip 等 | ✅ | 内部 `(路径, 大小, SHA-256)` 清单 | 识别重新压缩、重新打包 |
| 其他二进制 | ✅ | — | 只做精确去重 |

## 安装

系统依赖：

```bash
# ffmpeg/ffprobe: 视频抽帧
# libchromaprint-tools: 音频指纹 fpcalc（可选）
sudo apt-get update
sudo apt-get install -y ffmpeg libchromaprint-tools
```

Python 依赖（Python >= 3.11）：

```bash
# Debian/Ubuntu 若缺少 venv：sudo apt-get install -y python3-venv
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
# 或安装开发依赖
python3 -m pip install -e ".[dev]"
```

不装 `fpcalc` 也能运行，音频文件会显示为 `unsupported` 并只参与精确去重。

## 快速开始

```bash
# 全量：精确 + 结构指纹
dedup scan /data/media \
  --types image,video,document \
  --mode all \
  --report /data/reports/latest.json

# 只做精确去重，速度最快
dedup scan /data/media --mode exact --report /data/reports/exact.json

# 大目录先用小样本验证（按遍历顺序取前 N 个，提前停止遍历）
dedup scan /data/media --limit 500 --types image

# 查看报告（Markdown 输出到终端）
dedup report --report /data/reports/latest.json

# 清理：默认 dry-run，只打印计划
dedup apply --report /data/reports/latest.json

# 确认无误后真正移入 trash
dedup apply --report /data/reports/latest.json --yes
```

`python -m dedup ...` 与 `dedup ...` 等价。

## 图形界面（推荐普通用户）

不需要记命令，启动本地 Web 控制台后在浏览器里操作：

```bash
# 打开 http://127.0.0.1:8765/
dedup web --open

# NAS 上只允许访问媒体目录（推荐）
dedup web --root /volume1/media --open
```

Web 控制台使用 Python 标准库，不需要 Node.js 或额外前端构建。

安装完依赖后，也可以直接运行项目根目录的启动脚本（Windows 双击 `start-web.bat`）：

```bash
./start-web.sh --root /volume1/media
```

界面流程：

1. 点“浏览…”选择文件夹，勾选要扫描的内容类型。
2. 选择“标准模式”（精确 + 相似）或“仅完全重复”。
3. 开始扫描；页面会显示实时进度，可随时取消。
4. 在结果页查看缩略图、相似距离和每个文件的大小/时间。
5. 每组默认保留报告建议的文件；点击其他行的“保留”可更换 keeper。
6. 勾选要移走的重复文件，核对数量与可回收空间。
7. 确认后移动到回收站；操作前会再次校验文件是否变化。

其他能力：

- 历史报告：所有 Web 扫描报告保存在报告目录中，可随时重新打开审核。
- 缓存页：查看增量缓存状态、清理失效缓存。
- 图片/视频缩略图：服务端按需生成并缓存；文档/文本/音频显示类型图标。
- 目录限制：`--root /data` 会把浏览、扫描和报告访问限制在该目录下。
- 默认只监听 `127.0.0.1`。若用 `--host 0.0.0.0` 暴露到局域网，请配合防火墙；当前版本没有登录认证。

```bash
# 从群晖/局域网其他设备访问（务必设置 --root 和防火墙）
dedup web --host 0.0.0.0 --port 8765 --root /volume1/media
```

Web 模式默认使用：

```text
.dedup/cache.sqlite3     增量缓存
.dedup/reports/          Web 扫描报告
.dedup/thumbnails/       缩略图缓存
```

可用 `--db`、`--reports`、`--thumbs` 覆盖。所有“移动”只会进入 `<扫描根>/.dedup-trash/<时间戳>/`，不会永久删除文件。

## 命令说明

### `scan`

| 参数 | 说明 |
|---|---|
| `ROOT` | 扫描根目录 |
| `--config` | YAML 配置；不传时自动读取当前目录的 `config.yaml` |
| `--types` | `image,video,audio,document,text,archive,binary` 的子集，默认全部 |
| `--mode` | `exact`、`fingerprint`、`all`；`all` 当前等同于前两层，语义层启用后才会不同 |
| `--db` | SQLite 缓存路径，默认 `.dedup/cache.sqlite3` |
| `--report` | JSON 报告路径（相邻生成 `.md`），默认 `.dedup/reports/latest.json` |
| `--limit` | 限制处理文件数；按目录/文件名排序的遍历顺序取前 N 个，提前停止遍历 |
| `--workers` | 并行哈希/指纹线程数；NAS 上建议 1–2 |
| `--min-size` | 跳过小于 N 字节的文件 |
| `--follow-symlinks` | 跟随符号链接；apply 会拒绝移动符号链接，仅建议用于分析 |
| `--refresh` | 忽略缓存，强制重算 |
| `--no-semantic` | 显式关闭语义层（当前默认关闭） |

### `report`

渲染保存的 JSON 报告，`--top` 控制展示的组数，`--out` 可写入 Markdown 文件。

### `apply`

- **默认 dry-run**，只显示将移动哪些文件；dry-run 只校验存在性/大小/mtime，不做内容哈希。
- 必须显式 `--yes` 才会移动。
- 移动前重新校验：文件存在、大小、mtime，以及（默认）SHA-256 内容未变化。校验失败会跳过并要求重新扫描。
- 移动到 `<root>/.dedup-trash/<timestamp>/`，保留原相对路径；同时写 `manifest.json` 和逐条 `journal.jsonl`。
- 永不删除文件。

```bash
# 自定义 trash 目录（建议放在扫描根目录之外，或加入 scan.exclude）
dedup apply --report report.json --yes --trash /volume1/trash/content-dedup

# 跳过内容哈希校验（不建议；仍会检查 size/mtime）
dedup apply --report report.json --yes --no-verify
```

### `cache`

```bash
dedup cache stats
dedup cache prune      # 删除已不存在文件的缓存
dedup cache clear --yes
```

### `web`

```bash
dedup web [--host 127.0.0.1] [--port 8765] [--root DIR] [--open]
```

启动本地 Web 控制台。`--root` 会限制浏览器可浏览/扫描的范围；`--open` 自动打开默认浏览器。详细流程见上文。

## 分层与阈值

### 图片

- pHash：32×32 灰度 DCT，取低频 8×8 生成 64 位指纹。
- dHash：通过 `image.algorithm: dhash` 切换。
- 颜色校验：额外保存 4×4×4 RGB 直方图；pHash/dHash 通过后，颜色直方图交集低于 `image.color_threshold` 仍判为不相似。这能避免“同构图不同配色”被误删，但会拒绝强色调滤镜/调色后的版本，可按需调低阈值。

### 视频

- 按 `video.frame_interval` 抽帧；帧数超过 `max_frames` 时自动放大采样间隔，覆盖整段视频。
- 每帧 pHash，序列用带窗口的 DTW 对齐，平均汉明距离 ≤ `video.phash_threshold` 判为相似。
- 每帧同时累计 4×4×4 RGB 直方图，颜色相似度低于 `video.color_threshold` 直接拒绝；可避免纯色/静态画面 pHash 全相同导致误判。
- 时长比例低于 `1 - video.duration_tolerance` 直接拒绝；无法从 ffprobe 获取时长时仅做精确去重。
- 首尾剪辑、明显插帧可能降低相似度；这是为安全删除做的保守取舍。
- `max_pair_comparisons` 限制同一批视频的两两比较次数，防止超大视频库退化为不可接受的 O(n²)；达到上限会在报告中告警。

### 音频

- 调用 `fpcalc -json -raw -length N` 获取 Chromaprint 原始指纹。
- 在 ±`audio.max_shift` 帧内平移对齐，重叠至少 `audio.min_overlap` 帧，位错误率换算为相似度，≥ `audio.min_score` 判为相似。
- 时长差超过 `audio.duration_tolerance` 直接拒绝。
- `max_pair_comparisons` 同样用于限制超大音频库的两两比较次数。

### 文档 / 文本

- PDF 用 `pypdf`，DOCX 用 `python-docx`，XLSX 用 `openpyxl`；PPTX/ODF/EPUB 直接解析容器 XML。
- 文本 token 化后：unigram 权重 1，3-token shingle 权重 2，生成 64 位 SimHash。
- 汉明距离 ≤ 对应类型的 `simhash_hamming` 判为相似；默认 3。
- 少于 `min_tokens` 的文档/文本只做精确去重，避免短文本误判。
- 文本文件超过 `text.max_bytes` 时只做精确去重。
- 文档提取文本超过 `max_chars`、或 PDF 页数超过 `max_pages` 时只做精确去重，避免只比较前缀造成误判。

### 压缩包

- zip/tar 读取每个成员内容并计算 SHA-256，清单按 `(相对路径, 大小, SHA-256)` 排序后哈希。
- 单文件 `.gz/.bz2/.xz` 对解压后的内容做哈希。
- 因此“同内容、不同压缩级别、不同打包顺序”的压缩包会得到相同指纹。
- 解压总量、条目数、单文件大小都有上限，防止压缩炸弹；超限时只做精确去重。
- 当前支持 zip / tar / tar.gz / tgz / tar.bz2 / tar.xz / gz / bz2 / xz；7z、rar、zst 等作为普通二进制精确去重。

## 配置

完整默认值见 [`config.yaml`](config.yaml)。常用项：

```yaml
scan:
  workers: 1
  min_size: 1
  exclude: [".git", "@eaDir", "#recycle", ".dedup-trash"]

image:
  algorithm: phash          # phash | dhash
  phash_threshold: 8        # 0..64
  color_threshold: 0.85     # 直方图交集

video:
  frame_interval: 5.0
  max_frames: 240
  phash_threshold: 10
  duration_tolerance: 0.2
  color_threshold: 0.8
  max_pair_comparisons: 200000

audio:
  enabled: true
  min_score: 0.9
  duration_tolerance: 0.15
  max_pair_comparisons: 200000

document:
  simhash_hamming: 3
  min_tokens: 20

text:
  simhash_hamming: 3
  min_tokens: 20
  max_bytes: 4000000

safety:
  keep: oldest              # oldest | newest | largest | shortest_path
  trash_dir: .dedup-trash
  verify_on_apply: true

storage:
  database: .dedup/cache.sqlite3
  report: .dedup/reports/latest.json
```

距离/相似度阈值只影响分组，不触发指纹重算；影响指纹内容或可用性的参数（如 `hash_size`、`min_tokens`、`frame_interval`）会自动使缓存失效。

## 增量缓存

SQLite 表 `files` 保存：

```text
path, size, mtime_ns, adapter, exact_hash,
fingerprints(JSON), fp_status, fp_error,
fp_sig, cache_version, updated_at
```

- 文件路径、大小、mtime、适配器、算法签名都不变时直接复用。
- `mode=exact` 只缓存精确哈希；之后跑 `mode=all` 会复用缓存的 SHA-256、只补算指纹。
- 指纹失败/不支持也会缓存，避免 NAS 每次重复付出代价；用 `--refresh` 强制重试。
- 外部工具可用性（`fpcalc`、ffmpeg/ffprobe）和影响指纹值的参数都包含在签名中；安装工具或改参数后会自动失效。
- 文件在扫描过程中发生变化则不会写缓存。
- SQLite 缓存建议放在本地磁盘或 NAS 的本地卷上；WAL 模式在网络文件系统上可能不可靠。不要同时运行多个 scan 进程写同一个缓存。

## 报告与安全

JSON 报告包含：

- `summary`：组数、重复文件数、可回收空间。
- `groups`：每组 keeper、重复文件、判定类型、汉明距离/相似度、保留依据。
- `fingerprint_issues`：按适配器聚合的失败/不支持原因（如缺少 `fpcalc`、文本过短）。
- `settings`：本次运行的完整配置，便于审计。

安全策略：

- 默认 dry-run。
- 只移动重复项，绝不移动 keeper。
- 保留相对路径，写入 manifest/journal。
- apply 前校验 size/mtime/SHA-256。
- 压缩包和二进制只做精确去重，不做“相似删除”。
- NAS 上建议执行前打 Btrfs 快照，并把 trash 放在可回滚的卷上。

## 架构

```text
src/dedup/
├── adapters/
│   ├── base.py          # ContentAdapter 抽象、MatchInfo
│   ├── image.py         # pHash/dHash + 颜色直方图
│   ├── video.py         # ffmpeg 抽帧 + pHash 序列 + DTW
│   ├── audio.py         # fpcalc / Chromaprint
│   ├── document.py      # PDF/Office/ODF/EPUB 文本提取
│   ├── text.py          # 文本/代码/标记语言
│   ├── archive.py       # 内部清单哈希
│   ├── binary.py        # 兜底
│   └── textutil.py      # tokenizer、编码检测、SimHash 构建
├── fingerprints.py      # pHash/dHash/SimHash/DTW/音频比对
├── index.py             # 分段汉明索引（banding）
├── pipeline.py          # 枚举、缓存、并发、编排
├── grouping.py          # 精确桶 + 指纹星型聚类
├── store.py             # SQLite 增量缓存
├── report.py            # JSON/Markdown
├── cleanup.py           # trash 安全移动、按选择清理
├── config.py            # 配置加载/校验
├── cli.py
└── web/                 # 本地 Web 控制台
    ├── server.py        # HTTP/JSON API
    ├── jobs.py          # 后台扫描/清理任务
    ├── thumbnails.py    # 图片/视频缩略图
    └── static/          # HTML/CSS/JS（无外部 CDN）
```

指纹索引使用“阈值 + 1 个分段”的 banding：若两个 64 位值的汉明距离 ≤ 阈值，至少一个分段完全相同（鸽巢原理）。先取候选，再用适配器精确校验，避免 O(n²) 全量比较。视频/音频按数量较少且带时长门控，使用排序后的区间扫描。

聚类采用 **星型聚类**：keeper 必须与组内每个重复项直接满足阈值，避免 A~B、B~C 但 A 不~C 的链式合并导致误删。

## 测试

```bash
python3 -m pip install -e ".[dev]"
pytest -q
# 跳过 ffmpeg 集成测试
pytest -q -m "not integration"
```

集成测试会用 ffmpeg 生成小视频并验证重编码后的匹配；音频匹配算法有合成指纹单测，缺少 `fpcalc` 时验证降级行为。Web 测试会启动真实 HTTP 服务，覆盖目录浏览、扫描任务、报告、缩略图和按选择清理。

## NAS / 大目录建议

1. **分阶段跑**：先 `--mode exact` 快速拿到完全重复；再按类型跑结构指纹；音频/视频单独错峰。
2. **限制并发**：`--workers 1`（默认）或 2；视频抽帧时更高并发会打满 NAS CPU。
3. **缓存和报告放在本地卷**：SQLite WAL 不建议放在网络文件系统；报告目录也应可写。
4. **执行前打快照**：Btrfs 快照 + `trash/` 双保险；先看 dry-run 和 Markdown 报告。
5. **按目录分批**：超大视频库可拆成多个目录分别扫描，避免触发 `max_pair_comparisons`。

示例：

```bash
# 阶段 1：完全重复
dedup scan /volume1/media --mode exact \
  --db /volume1/docker/content-dedup/db/cache.sqlite3 \
  --report /volume1/docker/content-dedup/reports/exact.json

# 阶段 2：图片 + 文档
dedup scan /volume1/media --types image,document,text,archive \
  --db /volume1/docker/content-dedup/db/cache.sqlite3 \
  --report /volume1/docker/content-dedup/reports/docs.json

# 阶段 3：视频/音频（低频时段）
dedup scan /volume1/media --types video,audio --workers 1 \
  --db /volume1/docker/content-dedup/db/cache.sqlite3 \
  --report /volume1/docker/content-dedup/reports/video.json
```

后续阶段会命中前序阶段写入的 SQLite 缓存，只补算对应类型的指纹。

## 当前限制

- 语义向量层未实现；`semantic.enabled: true` 会明确报错。
- 旧版二进制 Office（`.doc/.xls/.ppt`）只做精确去重。
- PDF/Office 仅提取已有文本层；扫描版 PDF 需要 OCR，当前只做精确去重。
- HEIC/HEIF/AVIF 需要对应的 Pillow 插件；缺失时只做精确去重。
- 动图/多页 TIFF 只取第一帧做感知指纹。
- SVG 按二进制处理，不参与图片 pHash；避免仅文本相似的不同矢量图被误删。
- 小于 `min_tokens` 的文本/文档只做精确去重。
- 音频结构匹配依赖系统安装 `fpcalc`；默认只分析前 `audio.length` 秒（120），超长音频或不同段落可能不匹配。
- 图片颜色校验偏保守，强调色版本可能需要调低 `image.color_threshold`（设为 0 等价于关闭颜色校验）。
- 视频结构匹配要求 ffprobe 能返回时长；无法获取时长时只做精确去重。
- 超大视频/音频库会触发 `max_pair_comparisons` 上限并在报告中告警；建议按目录或类型分批扫描。
- 自定义 trash 目录若位于扫描根目录内，请加入 `scan.exclude`，否则下次扫描会把 trash 内容当作数据；自定义报告/缓存路径同理。
- Web 控制台默认只监听 `127.0.0.1`，没有登录认证；绑定 `0.0.0.0` 前请确认网络可信并配置防火墙。
- Web 当前没有“一键恢复回收站”页面；移动后需从 `.dedup-trash` 手动取回。
- 本版本未包含 Dockerfile/NAS 镜像，`project_readme.md` 第 7 节的 Docker 化可作为下一阶段。
