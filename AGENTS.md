# AGENTS.md

Diffuse is a self-hosted GitHub pull-request review platform: it receives a PR
webhook, pins the exact head, runs isolated read-only Codex / Claude Code CLI
investigations, verifies their findings, and publishes a review and GitHub
Check. Python 3.12, FastAPI, PostgreSQL 17. Proprietary under
[BSL 1.1](LICENSE); no contribution process yet.

> `repository_policy/discovery.py` indexes this file as repo-wide guidance when
> Diffuse reviews its own repository. Keep it to durable, project-wide
> instructions; machine-specific setup lives in [docs/cursor-cloud.md](docs/cursor-cloud.md).

## Setup

Install order matters: the lock file first (pins exact versions), dev tooling
second (its ranges are then already satisfied), the package last with
`--no-deps` so pinned requirements are not re-resolved (DEV-318).

```bash
uv venv --seed .venv --python 3.12    # or: python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -r requirements-dev.txt
pip install --no-deps -e .
pip check                             # -> No broken requirements found.
```

## Tests

```bash
pytest -m "not integration"           # fast, no database, no network
pytest tests/test_database_migrations.py   # single file works the same way
```

Run the unit suite constantly; anything not marked `integration` must pass
without external services. Tests never load `.env`.

**A green unit run is not a full pass.** Migrations and every store are covered
only by `tests/integration/`, which needs PostgreSQL. Integration tests skip
silently when `POSTGRES_TEST_DATABASE_URL` is unset — check the summary for
`skipped` before trusting a green run.

```bash
cp .env.example .env    # fill every empty value in the top REQUIRED block;
                        # generation commands are in the inline comments.
                        # Compose interpolates the whole file even for one
                        # service, so partial .env files fail fast.
docker compose up -d db                              # wait until "(healthy)"
docker compose exec db createdb -U diffuse diffuse_test
export POSTGRES_TEST_DATABASE_URL="postgresql://diffuse:<POSTGRES_PASSWORD>@localhost:5432/diffuse_test"
DATABASE_URL="$POSTGRES_TEST_DATABASE_URL" diffuse database migrate
pytest -m integration
```

Before opening a PR — and whenever touching `subprocess`, filesystem paths, the
sandbox, or the mirror — run the suite inside the shipped image's platform and
dependency set (same image CI uses; disposable tmpfs database):

```bash
docker compose -f docker-compose.tests.yml run --rm tests
docker compose -f docker-compose.tests.yml down -v
```

## Lint

```bash
ruff check .          # CI runs exactly this; any finding fails
```

`ruff check --fix .` and `ruff format` are fine locally, but keep
formatting-only churn out of functional PRs.

## Database changes: never edit `sql/schema.sql`

`sql/schema.sql` is the frozen version-1 migration. Its SHA-256 is pinned in
`service/storage/migrations.py` and verified at catalog load — any edit, even
whitespace, is a hard failure. Add a numbered migration under `sql/migrations/`
instead; see [sql/migrations/README.md](sql/migrations/README.md) for the
naming and content rules. Never edit a migration after it ships: applied
migrations are checksum-verified and drift fails closed at startup.

## Dependencies

After intentionally changing a range in `requirements.txt`, regenerate and
review the lock:

```bash
pip-compile requirements.txt --output-file=requirements.lock \
  --generate-hashes --allow-unsafe --strip-extras
```

## Where to read more

- [docs/v1-scope.md](docs/v1-scope.md) — the active product boundary. Read it
  first whenever an older document or code comment conflicts with it.
- [docs/README.md](docs/README.md) — index of the remaining reference docs.
- [.env.example](.env.example) — environment variables, documented inline.
- [SECURITY.md](SECURITY.md) — vulnerability reporting and the security model.
- [docs/cursor-cloud.md](docs/cursor-cloud.md) — Cursor Cloud VM only: native
  PostgreSQL, no Docker, and that machine's gotchas.
