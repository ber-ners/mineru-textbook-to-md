---
name: mineru-textbook-to-md
description: Parse a structured PDF with MinerU, preserve Markdown and images, split it into reviewable sections, and create mapping and quality reports. Use when Codex needs to convert a PDF to Markdown, split existing MinerU full.md output, repair document boundaries, or prepare image-preserving Markdown.
---

# MinerU Structured PDF to MD

Convert an authorized structured PDF into `full.md`, a PDF-bookmark-derived outline, ordered section folders, local images, and review artifacts.

## Safety First

- Before any API upload, confirm the exact PDF, output location, split granularity, and that the user authorizes uploading the file to MinerU.
- Do not upload files containing personal, patient, confidential, regulated, or unauthorized copyrighted material.
- Use `MINERU_OFFICIAL_API_TOKEN` from the environment. Never print a token, read it into generated output, or ask the user to paste it into a repository file.
- Treat MinerU ZIP results and Markdown image links as untrusted. Use only the packaged Python CLI, which enforces archive and local-path boundaries.
- Do not overwrite raw or split output unless the user explicitly requests `--force` or `--archive-output`.

## Resolve Inputs

Collect or infer:

- `pdf_path`: exact input PDF, unless splitting an existing `full.md`.
- `book_title`: concise output title; default to a clean PDF stem.
- `split_by`: `auto` (default), `part`, `chapter`, or `section`.

Ask only for items that remain ambiguous. Echo the resolved inputs before starting. If multiple PDFs match, show candidates and wait for a selection.

When a PDF is available, read its embedded bookmarks before inferring structure from Markdown. Decode bookmark titles from their original PDF bytes, use them as the TOC framework, and match their chapter nodes to `full.md`. If the PDF has no usable bookmarks, preserve the Markdown TOC/body detection and legacy fallback splitter.

## Plan Semantic Upload Chunks First

Before uploading to MinerU, read the PDF bookmarks locally and create an ordered page-boundary tree (part, chapter, then section where present). Do not divide a book mechanically at page 200, 400, and so on when usable bookmarks exist.

- Treat bookmark start pages as the preferred legal upload cut points. Keep a chapter intact whenever possible.
- Use global balancing: choose the API-safe number of chunks `ceil(pageCount / pageRangeSize)` and select bookmark boundaries that minimize deviation from an even target chunk size.
- If a chapter would make a chunk exceed the API limit, use its child section bookmarks as legal cut points. Do not split through a chapter body solely because a fixed page counter is reached.
- If no bookmark/section boundary can satisfy the limit, stop before upload and report the oversized semantic unit for review; fixed-page ranges are only a documented fallback when the PDF has no usable bookmarks.
- Write `00-MinerU分块计划.json` before submitting. It must record the chunk method, page limit, target size, bookmark count, and every inclusive page range.
- Include the start-node title and source page for each range in the plan preview, so a human can verify every proposed boundary before upload.
- Preserve raw per-chunk MinerU ZIP extraction, Markdown, images, and layout JSON under `00-MinerU原始分块/`; never delete these artifacts after a successful merge. Reuse completed chunk directories after transient API failures and submit only missing ranges.
- Merge results in this planned range order and keep the range comments in `full.md`, so each later boundary can be traced back to a specific source PDF range.
- A successful page plan does not authorize writing chapter folders. The parsed chapter tree must still match the planned PDF bookmark nodes in count, order, parent part, and start-page/anchor evidence.

## Run the CLI

Install dependencies in a new environment:

```powershell
$env:PYTHONUTF8 = "1"
python -m pip install "<skill>"
```

Write next to the source PDF:

```powershell
python -m mineru_textbook_to_md `
  --pdf "<pdf>" --base-name "<title>" --split-by auto
```

The default upload path performs local bookmark preflight and writes the semantic chunk plan automatically. Review `00-MinerU分块计划.json` before relying on a multi-chunk result.

Plan without upload first:

```powershell
python -m mineru_textbook_to_md `
  --pdf "<pdf>" --base-name "<title>" --plan-only
```

Use `--force` only after reviewing this plan. It clears only MinerU cache files and never deletes
the source PDF or reports. Do not use `--allow-unverified-split` for routine processing; it exists
only for a deliberate, documented manual repair.

Split an existing result without uploading:

```powershell
python -m mineru_textbook_to_md `
  --from-full-md "<full.md-or-folder>" --page-count <pages> --dry-run
```

Remove `--dry-run` only after reviewing planned boundaries. Use `--force` only after the PDF changes. Use `--archive-output` only for a clean split-output rebuild.

When importing a `full.md` from another directory, an existing different
`full.md` in the destination is treated as a conflict. Pass `--force` to
replace it explicitly. MinerU result downloads follow system proxy settings
by default; use `--direct-download` only when direct CDN access is required.

## Output Contract

By default, create one book-named folder beside the source PDF and place the copied PDF, `full.md`, raw MinerU assets, reports, and reviewable chapter tree inside it. For example, `项目管理指南（2025版）/` contains every output artifact. Prefix part and chapter folders with stable numeric order, for example `02-第二篇 呼吸系统疾病/01-第一章 总论/`. Pass `--output` to choose the parent folder, which is created when needed.

The PDF root must include:

- `00-章节拆分预览.md`
- `00-PDF书签分级目录.md` when usable PDF bookmarks were found
- `00-解析质量总报告.md`
- `00-章节映射.json`
- `00-待人工复核.md`
- part folders named from the original PDF bookmark, containing chapter folders named from the original heading number and title; each chapter folder contains `01-原文解析.md`, `02-解析质量报告.md`, and `images/`

Keep the original hierarchy, for example `02-第二篇 呼吸系统疾病/01-第一章 总论/`. Chapter folders are written in ascending PDF-page order, with Markdown position as the fallback.

Do not hide unreliable boundaries. Mark unmatched or low-confidence TOC/body matches for review, and refuse to write formal chapter folders when semantic validation fails unless a deliberate `--allow-unverified-split` override is supplied. For a focused repair, read `references/workflow.md` and add `chapter-overrides.json` with `id` or `title` plus `startAnchor`, `startLine`, `startPage`, or `skip`.

## Verify and Report

Confirm `full.md` exists, the planned and parsed part/chapter counts match the PDF outline, every chapter boundary is strictly increasing, each chapter has a source page or a stable text anchor, and missing images are recorded. Return the raw Markdown path, split root, planned ranges, chapter count, copied-image count, missing-image count, and any override or review flag.
