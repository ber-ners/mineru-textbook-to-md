# Contributing

Thanks for improving mineru-textbook-to-md.

## Development setup

```bash
python -m pip install -e ".[dev]"
```

## Before a pull request

Run the same checks used by CI:

```bash
python -m unittest discover -s tests -v
python -m ruff check mineru_textbook_to_md tests
python -m build
python -m twine check dist/*
```

## Contribution rules

- Keep changes focused and preserve backward-compatible CLI behavior when practical.
- Add a regression test for parsing, path handling, overwrite behavior, or output safety changes.
- Use synthetic or redacted fixtures only.
- Never commit API tokens, real textbooks, MinerU ZIP results, extracted images, generated `full.md`, or local absolute paths.
- Document behavior, compatibility, privacy, and data-handling changes in the pull request.
- Update `CHANGELOG.md` for user-visible changes.

## Reporting problems

Use the issue templates for reproducible bugs and feature proposals. Report security issues privately according to `SECURITY.md`.
