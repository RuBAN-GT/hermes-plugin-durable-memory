# Contributing

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) for every
commit:

```text
type(scope): imperative summary
```

Use one of these types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`,
`build`, `ci`, `chore`, `style`, or `revert`.

- Keep the summary concise, in imperative mood, and without a trailing period.
- Use a lowercase imperative verb after the colon: `feat: add approval queue`.
- Add a scope only when it makes the affected area clearer:
  `fix(store): isolate private namespaces`.
- Add a body for security changes, breaking changes, migrations, or non-obvious
  rationale. Wrap body lines at 72 characters.
- Use `!` and a `BREAKING CHANGE:` footer for incompatible changes.

## Quality Checks

Run these checks before opening a pull request:

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```
