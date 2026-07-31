# Developing Diffuse

Diffuse is proprietary, source-available software under
[BSL 1.1](LICENSE) — not open source. This is the engineering guide:
environment setup, the test recipe, and the rules that are not obvious from the
code. There is no contribution process yet; see
[`SECURITY.md`](SECURITY.md) for vulnerability reporting and
[`LICENSE`](LICENSE) for what you may do with the source.

Every command below was executed against a real checkout before being written
down here.

## Prerequisites

- Python 3.12 (the version in `.python-version`)
- Docker with Compose v2 — only needed for the integration tests and for
  running the full stack
- Git

No model provider credentials or GitHub token are needed to run
the unit tests or the linter.

## Set up a development environment

Create a virtual environment and install the development dependencies plus the
package itself in editable mode. The editable install is what puts the
`diffuse` command on your `PATH`; `--no-deps` matches CI and keeps the pinned
runtime requirements from being re-resolved.

With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --seed .venv --python 3.12
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install --no-deps -e .
```

With the standard library's `venv`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install --no-deps -e .
```

`--seed` is required with `uv venv`: without it the environment has no `pip`
and the two `pip install` lines fail with `No module named pip`. If you prefer
to skip it, use `uv pip install` in place of `pip install`.

Confirm the install:

```bash
pip check          # -> No broken requirements found.
diffuse --help
```

## Run the unit tests

```bash
pytest -m "not integration"
```

Roughly 7-12 seconds, no database and no network. This is the suite to run
constantly while you work. Anything not marked `integration` must pass without
external services.

The suite is hermetic against a local `.env`. `litellm` calls `load_dotenv()`
on import, which would otherwise merge your `.env` into the test environment
and fail tests you did not touch; `tests/conftest.py` disables that for the
test process. You do not need to move `.env` aside.

## Run the linter

```bash
ruff check .
```

## Run the review-quality harness

`pytest` proves the review engine's plumbing; it says nothing about whether the
engine finds bugs, because every review test stubs the model call.
`scripts/eval.sh` is the other half: it runs the real engine over the labeled
fixtures in `evals/fixtures/` and compares the scored result against a golden.

```bash
./scripts/eval.sh
```

It needs `REVIEW_MODEL` and that provider's API key, and it costs money —
roughly 40 model calls per run. **It currently exits non-zero on any machine,
because no golden is committed:** capturing one requires live model calls. See
[`evals/CAPTURE.md`](evals/CAPTURE.md) for the capture procedure and
[`evals/README.md`](evals/README.md) for the fixture format.

Ruff is configured in `pyproject.toml` (`E`, `F`, `I`, `UP`, `B`, `SIM`, line
length 100, target `py312`). CI runs the exact same command and treats any
finding as a failure. `ruff check --fix .` and `ruff format` are fine locally,
but keep formatting-only churn out of functional pull requests.

## Run the integration tests

The tests marked `integration` need a real PostgreSQL 17 with the `pgvector`
extension available. `README.md` points here rather than restating this; this
is the whole recipe.

### 1. Start the database

`docker-compose.yml` already defines a `pgvector/pgvector:pg17` service. Compose
interpolates the entire file even when you start a single service, so `.env`
must exist and must supply `POSTGRES_PASSWORD` **and** `DIFFUSE_API_TOKEN`
(`DIFFUSE_PUBLIC_URL` already has a value in `.env.example`). Without them
Compose refuses to start with
`required variable DIFFUSE_API_TOKEN is missing a value`.

```bash
cp .env.example .env
# Edit .env and set POSTGRES_PASSWORD and DIFFUSE_API_TOKEN.
# Both are in the "REQUIRED" block at the top of the file.
# Use URL-safe values, for example: openssl rand -hex 32
#
# REVIEW_MODEL ships commented out, so that copying this file cannot hand you a
# model you never chose. Uncomment it (or name your own) before running a
# review; the worker refuses to start until you do.

docker compose up -d db
```

Wait for the container's health check to pass:

```bash
docker compose ps db     # STATUS should read "(healthy)"
```

### 2. Create a disposable test database

The integration tests create, migrate, and drop objects freely, and
`tests/integration/test_database_migrations_postgres.py` creates and drops
whole databases. Point them at a throwaway database, not at the `diffuse`
database your local stack uses.

```bash
docker compose exec db createdb -U diffuse diffuse_test
```

### 3. Migrate the disposable database

```bash
export POSTGRES_TEST_DATABASE_URL="postgresql://diffuse:$POSTGRES_PASSWORD@localhost:5432/diffuse_test"
DATABASE_URL="$POSTGRES_TEST_DATABASE_URL" diffuse database migrate
```

Substitute the `POSTGRES_PASSWORD` value you put in `.env`. The command prints
a `diffuse-database-status-v1` document; a successful run ends with
`"state": "versioned"` and `"pending": []`.

### 4. Run the tests

```bash
pytest -m integration
```

Roughly 18 seconds. `tests/integration/conftest.py` copies
`POSTGRES_TEST_DATABASE_URL` into `DATABASE_URL`, so application code under
test connects to the same disposable database. If
`POSTGRES_TEST_DATABASE_URL` is unset, every integration test is skipped rather
than failing — check for `skipped` in the summary before believing a green run.

### Tearing down

```bash
docker compose exec db dropdb -U diffuse diffuse_test
docker compose down          # add -v to delete the pgdata volume as well
```

### If port 5432 is already in use

Publish the container on another port and use it in the URL:

```bash
cat > docker-compose.override.yml <<'YAML'
services:
  db:
    ports: !override
      - "127.0.0.1:55432:5432"
YAML
docker compose up -d db
export POSTGRES_TEST_DATABASE_URL="postgresql://diffuse:$POSTGRES_PASSWORD@localhost:55432/diffuse_test"
```

`docker-compose.override.yml` is picked up automatically; delete it when you are
done. Do not commit it.

## Database changes: never edit `sql/schema.sql`

**This is the rule most likely to bite you.**

`sql/schema.sql` is the frozen version-1 migration (`0001_initial_schema`). Its
SHA-256 is pinned in `BASELINE_SCHEMA_SHA256` in `service/database_migrations.py`
and verified every time the migration catalog is loaded. Editing the file — even
adding a comment or a trailing newline — is a hard failure at load time, not a
test failure you can defer:

```
$ diffuse database status
diffuse: error: sql/schema.sql is the frozen version-1 migration and was edited; add a numbered migration instead
```

The catalog is loaded after the database connection, so a command that cannot
reach PostgreSQL reports the connection failure first and hides the drift. In
the unit suite it surfaces as exactly one failure,
`tests/test_database_migrations.py::test_frozen_baseline_catalog_is_packaged_and_contract_is_parseable`.

To change the schema, add a new file under `sql/migrations/`:

1. Name it `NNNN_short_name.sql` with the next consecutive four-digit version
   and a lowercase `snake_case` name — the catalog rejects gaps, duplicates,
   and any other filename shape.
2. Write plain SQL that runs inside a single transaction. The loader rejects
   `psql` backslash commands, explicit transaction control
   (`BEGIN`/`COMMIT`/`ROLLBACK`/`START TRANSACTION`), `VACUUM`, and
   `CREATE`/`DROP INDEX CONCURRENTLY`.
3. Keep it under 10 MiB and valid UTF-8 with no NUL bytes.
4. Never edit a migration after it ships. Applied migrations are
   checksum-verified against `diffuse_schema_migrations`, and drift fails
   closed at startup — `GET /ready` will not return `200`.
5. Apply and test it with the integration recipe above.

See `sql/migrations/README.md` and
[ADR 0036](docs/adr/0036-transactional-versioned-database-migrations.md).

## Dependencies

`requirements.txt` holds runtime ranges, `requirements-dev.txt` adds the test
and lint tooling, and `requirements.lock` is the hash-locked set installed into
production images. After intentionally changing a runtime range, regenerate and
review the lock file:

```bash
pip-compile requirements.txt \
  --output-file=requirements.lock \
  --generate-hashes \
  --allow-unsafe \
  --strip-extras
```

CI audits the lock file with `pip-audit -r requirements.lock --disable-pip`.

## Design docs and ADRs

Diffuse records its architectural decisions rather than re-litigating them in
review. Before proposing a change to indexing, retrieval, the review engine,
publication, authorization, or the database, read the relevant record:

- [`docs/adr/`](docs/adr/) — architecture decision records, the primary source
  of truth for *why* a subsystem is shaped the way it is
- [`docs/architecture.md`](docs/architecture.md) — target architecture. Its
  data-model and deployment-profile sections are explicitly labelled targets and
  describe tables and services that do not exist yet; `sql/schema.sql` plus the
  applied files in `sql/migrations/` are authoritative for the real schema
- [`docs/capabilities.md`](docs/capabilities.md) — what is implemented versus
  what is still planned
- [`docs/roadmap.md`](docs/roadmap.md) — delivery sequence
- [`docs/deployment.md`](docs/deployment.md) — single-server deployment

A change that contradicts an accepted ADR needs a new ADR in the same numbered,
append-only style, not an edit to the old one.
