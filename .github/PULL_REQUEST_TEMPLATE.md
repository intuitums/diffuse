## What and why

<!-- What changes, and what problem it solves. Link the issue or ADR. -->

## How to verify

<!-- The commands or steps a reviewer can run. -->

## Checklist

- [ ] `pytest -m "not integration"` passes
- [ ] `ruff check .` passes
- [ ] `pytest -m integration` passes, or the change cannot affect it
- [ ] Schema changes are a new file in `sql/migrations/`; `sql/schema.sql` is untouched
- [ ] Docs or ADRs updated, or not applicable
