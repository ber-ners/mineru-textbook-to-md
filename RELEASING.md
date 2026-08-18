# Releasing

1. Update `mineru_textbook_to_md/__init__.py` and move completed entries from `[Unreleased]` in `CHANGELOG.md` into a dated version section.
2. Run the full validation suite:

```bash
python -m pip install -e ".[dev]"
python -m ruff check mineru_textbook_to_md tests
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
```

3. Install the built wheel in a clean environment and run `mineru-textbook-to-md --version`.
4. Create an annotated tag such as `v0.1.0` and publish a GitHub Release with the generated wheel and source archive.
5. Publish to PyPI only after configuring trusted publishing for the repository.
