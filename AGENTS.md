# Development notes

## Temporary files

- Use the system temp directory for scratch files that can be discarded after a run. Use `tmp/` for scratch files that need to stay in the checkout and `artifacts/` for local logs, screenshots, and test evidence.
- If a tool writes new generated files into the checkout, add a scoped rule to the repository's `.gitignore`. Use `git check-ignore -v <path>` to confirm the rule.
- Commit maintained fixtures under `tests/fixtures/` and screenshots used by the README under `docs/screenshots/`. Review `git diff --cached --name-only` before committing to keep local output out of the change.
