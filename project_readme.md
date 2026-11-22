好的，把项目从“图片去重”扩展为 **多模态内容去重与相似识别**。核心变化是：把“哈希/指纹计算”抽象成可插拔的 **内容适配器（Adapter）**，统一走“精确哈希 → 感知/结构指纹 → 语义特征 → 索引检索”的分层流水线。下面给出扩展方案。

---

## 1. 扩展后的内容类型与识别策略

| 内容类型 | 精确匹配（第一层） | 感知/结构指纹（第二层） | 语义特征（第三层，可选） |
|---|---|---|---|
| **图片** | SHA-256 | pHash / dHash | CLIP / ResNet embedding |
| **视频** | SHA-256 | 抽帧 pHash 序列 + 时长/分辨率元数据 | 视频帧 CLIP 聚合 / VideoMAE |
| **音频** | SHA-256 | 音频指纹（Chromaprint / AcoustID） | CLAP / 音频 embedding |
| **文档（PDF/Office）** | SHA-256 | 文本提取 + SimHash / MinHash | 文本 embedding（BGE / E5） |
| **纯文本/代码** | SHA-256 | SimHash / MinHash / shingling | 文本 embedding |
| **压缩包** | SHA-256 | 内部文件清单哈希 + 大小结构 | 一般不做语义 |
| **其他二进制** | SHA-256 | 可选：ssdeep 模糊哈希 | 一般不做 |

关键设计原则：**不是所有类型都需要三层**。二进制/压缩包通常只做精确哈希；文档和文本重点在语义层；音视频重点在抽帧/指纹层。

---

## 2. 统一抽象：Adapter 插件架构

把原来 `exact_hash.py` / `perceptual.py` 改成按类型分发的适配器。

```text
src/imgdedup/
├── adapters/
│   ├── base.py          # ContentAdapter 抽象基类
│   ├── image.py         # 图片：sha256 + phash
│   ├── video.py         # 视频：sha256 + 抽帧phash序列
│   ├── audio.py         # 音频：sha256 + chromaprint
│   ├── document.py      # 文档：sha256 + 文本SimHash
│   ├── text.py          # 文本/代码：sha256 + SimHash
│   └── binary.py        # 兜底：sha256 (+ ssdeep)
├── pipeline.py          # 分层流水线调度
├── index.py             # 向量/指纹索引
└── report.py
```

**基类接口示例：**

```python
# adapters/base.py
from abc import ABC, abstractmethod
from pathlib import Path

class ContentAdapter(ABC):
    extensions: set[str] = set()

    @abstractmethod
    def exact_hash(self, path: Path) -> str:
        """字节级精确哈希，所有类型通用"""

    def perceptual_fingerprint(self, path: Path) -> str | None:
        """感知/结构指纹，默认不实现"""
        return None

    def semantic_embedding(self, path: Path) -> list[float] | None:
        """语义向量，默认不实现"""
        return None
```

**分发器：**

```python
ADAPTERS = [ImageAdapter(), VideoAdapter(), AudioAdapter(),
            DocumentAdapter(), TextAdapter(), BinaryAdapter()]

def get_adapter(path: Path) -> ContentAdapter:
    ext = path.suffix.lower()
    for a in ADAPTERS:
        if ext in a.extensions:
            return a
    return BinaryAdapter()  # 兜底
```

这样主流程完全不感知具体类型，新增类型只需加一个 Adapter。

---

## 3. 各类型指纹实现要点

### 视频：抽帧 + 序列比对

视频不能整体做 pHash，做法是**按固定间隔抽帧**（如每 5 秒一帧），对每帧算 pHash，得到指纹序列。相似判定用序列对齐（如动态时间规整 DTW）或简单的前 N 帧汉明距离均值。

```python
# 伪代码
def video_fingerprint(path, interval=5):
    frames = extract_frames(path, interval)  # ffmpeg
    return [phash(f) for f in frames]
```

依赖 `ffmpeg`，Docker 镜像里要加 `ffmpeg` 包。

### 音频：Chromaprint 指纹

用 `pyacoustid` / `chromaprint` 生成音频指纹，天然支持“同一首歌不同码率/剪辑”的匹配。

### 文档：文本提取 + SimHash

PDF 用 `pypdf`，Office 用 `python-docx` / `openpyxl`，提取纯文本后计算 **SimHash**（64 位），汉明距离 ≤ 3 视为近似。对“同一文档不同格式导出”特别有效。

```python
from simhash import Simhash
def doc_fingerprint(text: str) -> int:
    return Simhash(text.split()).value
```

### 文本/代码：SimHash 或 MinHash

长文本用 SimHash，短文本/集合用 MinHash + Jaccard。

### 压缩包：内部清单哈希

解压列出 `(相对路径, 大小, 内部文件sha256)` 排序后再哈希，能识别“重新打包但内容相同”的压缩包。

---

## 4. 流水线与索引的变化

**流水线**变成按适配器能力分层执行：

```python
for path in scan(root):
    adapter = get_adapter(path)
    rec = {"path": path, "type": adapter.__class__.__name__}
    rec["exact"] = adapter.exact_hash(path)
    rec["fingerprint"] = adapter.perceptual_fingerprint(path)
    rec["embedding"] = adapter.semantic_embedding(path) if enable_semantic else None
    store(rec)
```

**索引层**要支持多种距离：
- 精确哈希：字典分组，O(n)
- pHash/SimHash：汉明距离，用 BK-tree 或分桶
- 向量 embedding：FAISS / HNSW，余弦相似度

不同指纹不能混在一个索引里，按 `(类型, 指纹种类)` 建独立索引。

---

## 5. 项目结构调整

```text
content-dedup/
├── Dockerfile              # 需加 ffmpeg、libmagic
├── config.yaml             # 按类型配置阈值
├── src/dedup/
│   ├── adapters/
│   ├── pipeline.py
│   ├── index.py
│   ├── store.py            # SQLite 缓存增量结果
│   ├── cli.py
│   └── report.py
└── tests/
```

配置文件按类型分阈值：

```yaml
image:
  phash_threshold: 8
video:
  frame_interval: 5
  phash_threshold: 10
audio:
  min_score: 0.9
document:
  simhash_hamming: 3
text:
  simhash_hamming: 3
semantic:
  enabled: true
  cosine_threshold: 0.92
```

---

## 6. 本地运行的变化

- 依赖变重：`ffmpeg`、`pypdf`、`python-docx`、`simhash`、可选 `torch`/`open_clip`。
- 建议用 `pip install -e ".[video,doc,semantic]"` 分组安装，避免一次装全。
- 本地调试仍用 `--limit` 和小样本目录，视频抽帧和语义模型最吃资源，先单独测。
- 语义层如果本地无 GPU，用 CPU 跑 CLIP 会很慢，建议本地只验证前两层。

```bash
# 仅精确+结构指纹，不启用语义
dedup scan ~/data --types image,video,document --no-semantic
```

---

## 7. NAS 运行的变化

**Docker 镜像要加系统依赖：**

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libjpeg62-turbo-dev zlib1g-dev libmagic1 \
    && rm -rf /var/lib/apt/lists/*
```

**资源限制更重要**。视频抽帧和语义推理是重负载，NAS 上务必限制：

```bash
docker run --rm \
  --cpus="2" --memory="2g" \
  -v /volume1/media:/data/media:ro \
  -v /volume1/docker/content-dedup/reports:/data/reports \
  -v /volume1/docker/content-dedup/db:/data/db \
  content-dedup:latest \
  scan /data/media --types image,video,document --no-semantic \
  --report /data/reports/latest.json
```

**分阶段跑**比一次全量更稳：
1. 先只跑 `--mode exact`，快速拿到完全重复清单；
2. 再按类型分批跑结构指纹（如今晚只跑视频）；
3. 语义层单独用夜间低峰跑，或干脆放在有 GPU 的机器上。

**增量是 NAS 上的生命线**。用 SQLite 缓存 `path + mtime + size + exact_hash + fingerprint`，下次只处理新增/修改文件。否则每次全量重算 pHash/抽帧，NAS 扛不住。

**调度**：群晖任务计划或 cron 分类型错峰执行，脚本里用绝对路径，输出日志到持久化目录。

---

## 8. 数据安全（比图片阶段更关键）

扩展到视频、文档后，误删代价更大：

- 默认 `--dry-run`，只出报告；
- 写操作先移动到 `trash/` 并保留原相对路径；
- 报告里记录每组“保留哪个、移动哪些”及判定依据（哈希距离/相似度）；
- NAS 上配合 Btrfs 快照，执行前打快照；
- 压缩包和二进制只做精确去重，不做“相似删除”，降低误判风险。

---

### 总结

扩展的关键不是给每种类型写一套流程，而是 **Adapter 抽象 + 分层流水线 + 按类型建索引**。图片阶段的三层策略原样泛化：精确哈希全类型通用；结构指纹按类型选算法（pHash / 抽帧序列 / Chromaprint / SimHash）；语义层只对图片、音视频、文档按需启用。本地负责调试和轻量验证，NAS 上用 Docker 加资源限制、增量缓存和分阶段调度，才能扛住多模态内容的规模。
