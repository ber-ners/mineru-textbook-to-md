# Changelog

All notable changes to this project are documented here. The format follows Keep a Changelog, and releases use Semantic Versioning.

## [Unreleased]

## [0.1.0] - 2026-08-18

### Added

- MinerU PDF upload, polling, safe ZIP extraction, Markdown merging, semantic splitting, image localization, mapping, and quality reports.
- Standard installable Python package and `mineru-textbook-to-md` console command.
- PDF-bookmark-aware upload planning and chapter alignment.
- Synthetic PDF integration coverage and output-safety regression tests.
- Resumable MinerU chunk processing after transient API failures.
- GitHub issue templates, pull request template, dependency updates, CI, and package build checks.

### Changed

- Unified the supported implementation around the Python CLI.
- Made proxy bypass explicit through `--direct-download`.
- Improved large-file conflict checks with streaming SHA-256 hashing.
- Made `--force` preserve unknown user files and original reports.
- Removed split page-number artifacts from exported PDF part bookmarks.

### Removed

- Project-specific scripts containing textbook-specific chapter counts and manual anchors.
- Duplicate legacy Node.js implementation and Obsidian-specific configuration dependency.
