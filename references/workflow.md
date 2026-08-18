# Workflow Reference

Use this reference only when a run needs command details beyond `SKILL.md`.

## Inputs and Consent

Resolve the exact PDF, title, output location, and split granularity before running. Before submitting a PDF, confirm that the user authorizes uploading the file to MinerU and that it contains no data prohibited by their privacy, copyright, or organization policies.

Never print or persist a token. Use `MINERU_OFFICIAL_API_TOKEN` (or the variable named by `--api-token-env`).

For a PDF workflow, extract embedded bookmarks before Markdown structure detection. Use the bookmark tree as the initial TOC framework, then align chapter nodes to `full.md`. If no usable bookmarks exist, keep the Markdown TOC/body matcher and legacy fallback path.

## Commands

Install dependencies:

```powershell
$env:PYTHONUTF8 = "1"
python -m pip install "<skill>"
```

Default output root: create one book-named folder under the source PDF's parent folder. The copied PDF, `full.md`, raw MinerU assets, reports, and chapter tree all live under that root, with numeric prefixes for part and chapter ordering. Use `--output` to choose a different parent folder, which is created when needed.

```powershell
python -m mineru_textbook_to_md `
  --pdf "<pdf>" --base-name "<title>" --split-by auto
```

Split an existing result without uploading:

```powershell
python -m mineru_textbook_to_md `
  --from-full-md "<full.md>" --page-count <pages> --dry-run
```

Remove `--dry-run` only after reviewing the planned chapter boundaries. Use `--force` only for a changed source PDF; it clears MinerU cache artifacts but preserves the source PDF and reports. Use `--archive-output` only when a complete output rebuild is intended.

When `--from-full-md` points outside the destination book folder, an existing
different destination `full.md` is a hard conflict. Add `--force` to replace it.
MinerU CDN downloads honor system proxies by default; `--direct-download` is
an explicit network workaround.

## Expected Output

- Raw result: `<PDF根目录>/full.md`
- Split result: `<PDF根目录>/<章节或小节目录>/`
- Bookmark framework: `00-PDF书签分级目录.md` when bookmarks are available
- Section folders keep original heading names with stable numeric prefixes and are emitted in PDF-page order, then Markdown position.
- Audit files: `00-章节拆分预览.md`, `00-解析质量总报告.md`, `00-章节映射.json`, `00-待人工复核.md`

When a chapter boundary is wrong, place `chapter-overrides.json` beside `full.md` or pass it with `--overrides`. Each override uses an `id` or `title` plus one of `startAnchor`, `startLine`, `startPage`, or `skip`.

## Validation

After a run, confirm that `full.md` exists, chapter order matches the table of contents, and the quality report records zero missing images. Report all low-confidence boundaries rather than silently correcting them.

