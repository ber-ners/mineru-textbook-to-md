<div align="center">

# MinerU Textbook Markdown Skill

一个面向 Agent 的 Skill：将结构化 PDF 转换为保留图文、按章节排序、可人工复核的 Markdown 文档包。

本仓库的核心入口是 `SKILL.md`，不是 Python 包。底层 Python 代码作为 Skill 的内置运行资源提供。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-f2c94c?style=flat-square)
![Status](https://img.shields.io/badge/status-alpha-e67e22?style=flat-square)

</div>

## 功能

- 调用 MinerU 官方 API 解析 PDF，保留 Markdown、图片和原始分块结果。
- 优先使用 PDF 书签规划上传边界，避免把章节机械切断在固定页数处。
- MinerU 临时失败后复用已完成分块，只重试尚未完成的页段。
- 对齐书签、目录和正文标题，支持篇、部、编、卷、单元、章、节等结构。
- 生成带稳定数字前缀的篇/章目录，并只复制章节实际引用的图片。
- 输出章节映射、拆分预览、质量报告和人工复核清单。
- 支持直接拆分已有的 `full.md`，不重复上传 PDF。
- 默认采用非破坏式文件管理；原始 PDF 和未知用户文件不会被清理。

> 当前版本为 `0.1.0`，适合试用和协作开发。正式处理重要文档前，请先使用 `--plan-only` 和 `--dry-run` 检查结果。

## 安装 Skill

将整个仓库放入 Agent 的 Skill 目录，例如：

```text
$CODEX_HOME/skills/mineru-textbook-to-md/
```

也可以直接下载 GitHub Release 中的 `mineru-textbook-to-md-skill-v*.zip` 并解压到该目录。

运行时要求 Python 3.10 或更高版本。首次使用时安装 Skill 依赖：

```powershell
python -m pip install -r "<skill-root>\requirements.txt"
```

配置 MinerU Token：

```powershell
$env:MINERU_OFFICIAL_API_TOKEN = "sk-..."
```

```bash
export MINERU_OFFICIAL_API_TOKEN="sk-..."
```

不要把 Token 写入仓库、命令示例或生成文件。Windows 终端出现中文乱码时，可先执行 `$env:PYTHONUTF8 = "1"`。

## 使用

通过 Skill 自带入口运行，不依赖固定路径：

```powershell
python "<skill-root>\scripts\run.py" --pdf "<pdf>" --plan-only
```

确认计划后解析并拆分：

```bash
python "<skill-root>/scripts/run.py" --pdf /path/to/book.pdf --split-by auto
```

拆分已有 `full.md`，先预览边界：

```bash
python "<skill-root>/scripts/run.py" \
  --from-full-md /path/to/full.md \
  --page-count 500 \
  --dry-run
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--output DIR` | 指定文档包的父目录 |
| `--base-name NAME` | 指定书名目录名称 |
| `--split-by VALUE` | 指定 `auto`、`part`、`chapter` 或 `section` |
| `--plan-only` | 仅生成上传分块计划 |
| `--dry-run` | 仅预览章节拆分，不写文件 |
| `--archive-output` | 写入前归档旧章节和报告 |
| `--force` | 显式替换冲突的 PDF/full.md，并重新生成 MinerU 缓存 |
| `--direct-download` | 仅下载 MinerU 结果时绕过系统代理 |

运行 `python "<skill-root>/scripts/run.py" --help` 查看全部参数。

## 输出

默认在源 PDF 同级创建一个书名目录：

```text
<PDF 所在目录>/
└── <书名>/
    ├── <原始 PDF>.pdf
    ├── full.md
    ├── 00-MinerU分块计划.json
    ├── 00-MinerU原始分块/
    ├── 00-PDF书签分级目录.md（有可用书签时）
    ├── 00-章节拆分预览.md
    ├── 00-章节映射.json
    ├── 00-解析质量总报告.md
    ├── 00-待人工复核.md
    ├── images/
    └── 02-第二篇 .../
        └── 01-第一章 .../
            ├── 01-原文解析.md
            ├── 02-解析质量报告.md
            └── images/
```

没有可用篇级书签的章节会进入 `99-未归类章节/`。工具不会为了目录外观创建没有正文的空章节。

## 文件安全

- `--force` 只清理工具拥有的 MinerU 缓存，并显式替换冲突的源副本；不会删除未知用户文件。
- 外部 `full.md` 或同名 PDF 与目标内容不同时，默认停止并要求确认。
- ZIP 解压限制路径、文件数、单文件大小和总展开大小。
- Markdown 中的绝对路径和越界图片引用不会被复制。
- 默认遵循系统代理；只有显式使用 `--direct-download` 才绕过代理下载结果。

## 数据与版权

运行 PDF 工作流会将文档上传到 MinerU。只处理你有权上传和转换的文件，不要上传患者信息、个人隐私、机密材料或受监管数据。项目只提供格式转换能力，不授予任何文档内容的复制或分发权利。

## 开发

```bash
python -m pip install -e ".[dev]"  # 仅开发者需要；普通 Skill 使用者安装 requirements.txt 即可
python -m unittest discover -s tests -v
python -m ruff check mineru_textbook_to_md tests
python -m build
python -m twine check dist/*
```

贡献指南见 `CONTRIBUTING.md`，安全问题见 `SECURITY.md`，版本变化见 `CHANGELOG.md`。

## License

MIT License. See `LICENSE`.
