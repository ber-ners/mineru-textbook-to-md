## Summary

- 

## Validation

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m ruff check mineru_textbook_to_md tests`
- [ ] `python -m build`
- [ ] `python -m twine check dist/*`

## Data safety

- [ ] No tokens, real PDFs, extracted content, generated output, or private paths are included.
- [ ] User-visible changes are documented in `CHANGELOG.md`.
