# Contributing to ContentDedup

感谢你愿意改进 ContentDedup。这个项目优先考虑三件事：**安全、可预测、普通用户可用**。

## 开发环境

```bash
git clone https://github.com/yejunhui/content-dedup.git
cd content-dedup

python3 -m venv .venv
. .venv/bin/activate

pip install -e ".[dev]"

# 可选：视频缩略图/抽帧与音频指纹
sudo apt-get install -y ffmpeg libchromaprint-tools
```

## 提交前检查

```bash
ruff check src tests
pytest -q
node --check src/dedup/web/static/app.js   # 修改前端时需要
```

- 不要求 100% 覆盖率，但新增功能必须附带测试。
- 涉及删除/移动文件的行为必须默认 dry-run，并保留校验步骤。
- 不要提交 Token、私钥、真实媒体文件或包含个人路径的截图。

## 代码结构

| 路径 | 作用 |
|---|---|
| `src/dedup/adapters/` | 按内容类型实现精确哈希与结构指纹 |
| `src/dedup/pipeline.py` | 扫描、缓存、进度与取消 |
| `src/dedup/grouping.py` | 精确桶 + 星型聚类 |
| `src/dedup/cleanup.py` | 安全移动到回收站 |
| `src/dedup/web/` | 本地 Web 控制台与 API |
| `tests/` | 单元测试与端到端测试 |

## 新增一种内容类型

1. 在 `adapters/` 下实现 `ContentAdapter` 子类。
2. 声明 `name`、`extensions`、`fingerprint_kinds`。
3. 实现 `compute_fingerprint()` 和 `match()`。
4. 在 `adapters/__init__.py` 注册。
5. 增加缓存签名、阈值配置、测试与 README 能力矩阵。

新增适配器必须遵循“只能更保守，不能更激进”的原则：宁可漏掉相似项，也不要把不同内容判为重复。

## Pull Request

- 一个 PR 只做一件事，描述动机、行为变化和验证方式。
- UI 变化请附截图。
- 提交信息建议使用 `feat:`、`fix:`、`docs:`、`test:`、`chore:` 前缀。
- 如果改变了用户可见行为，请同步更新 `README.md`、`README.en.md` 和 `config.yaml` 注释。

## 维护者发布清单

仓库描述、Topics、社交预览图和 Release 文案见 [docs/repository-metadata.md](docs/repository-metadata.md)。

## 安全问题

不要公开提交安全漏洞。请参考 [SECURITY.md](SECURITY.md)。
