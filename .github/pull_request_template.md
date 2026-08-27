## Checklist

- [ ] `pytest -m "not integration"` passes and `ruff check .` is clean
- [ ] The image suite passes (`docker compose -f compose.tests.yaml run --build --rm tests`) when the change touches `subprocess`, filesystem paths, the sandbox, or the mirror
- [ ] Migrations are added under `packages/server/migrations/` (never edit `schema.sql`); `diffuse database migrate` applies cleanly
- [ ] No secrets are committed and `.gitleaks.toml` coverage is intact
- [ ] Comments and docs touched by this change are updated, and `AGENTS.md` still matches reality
