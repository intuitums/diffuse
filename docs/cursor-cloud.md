# Cursor Cloud VM

> **Scope: one environment — the Cursor Cloud VM.** Nothing here generalises:
> there is no Docker, PostgreSQL runs natively, and the paths and credentials
> are that VM's. Everywhere else, [AGENTS.md](../AGENTS.md) applies.

## Repo-managed environment (`.cursor/`)

Cloud Agents resolve configuration from `.cursor/environment.json` first:

- `install` → `.cursor/install.sh` — ensure PostgreSQL 17 + pgvector packages,
  then create/refresh `.venv` from the locked requirements. Dependency refresh
  only; terminates; does not start the database.
- `start` → `.cursor/start.sh` — start PostgreSQL 17, ensure the `diffuse`
  role + databases, generate `.env` with dev values when absent, create the
  isolated git `HOME` and `/var/lib/diffuse/repositories`.

Do not put the API or worker in `install` or `terminals` by default: they need
`REVIEW_AGENT` / dispatch secrets and the rewrite-free git `HOME` below. Start
them on demand when the task needs a live stack.

## Services (native, no Docker)

- **PostgreSQL 17** — native cluster `17/main` on `127.0.0.1:5432`. Role
  `diffuse` (password `diffuse-dev`, `SUPERUSER`), databases `diffuse` (dev)
  and `diffuse_test` (integration tests). Not auto-started on a fresh pod boot
  (no systemd): `sudo pg_ctlcluster 17 main start` (check with `pg_lsclusters`).
  Nothing uses pgvector any more, but the frozen version-1 baseline still runs
  `CREATE EXTENSION IF NOT EXISTS vector` before a later migration drops it, so
  the extension must stay installed and the `diffuse` role stays `SUPERUSER`.
- **API/app** — `uvicorn service.hosted.webhook_server:app` on
  `127.0.0.1:8000` (GitHub webhooks, private runner transport, `/health`,
  `/ready`). Single FastAPI process.
- **worker** — `python -m service.hosted.worker` (leases jobs from the
  Postgres queue; no separate broker/cache).

Python dependencies live in `.venv`; use `.venv/bin/...` or activate it.

## Tests on this VM

Tests do not load `.env`, but do not run `pytest` from a shell where you have
already done `set -a; source .env` (how the app/worker are launched). App in
one shell, tests in a separate un-sourced shell.

```bash
.venv/bin/python -m pytest -m "not integration"
POSTGRES_TEST_DATABASE_URL=postgresql://diffuse:diffuse-dev@127.0.0.1:5432/diffuse_test \
  .venv/bin/python -m pytest -m integration        # migrate the test DB first
```

## Gotcha — run the app + worker with an isolated git `HOME`

The VM's global `~/.gitconfig` contains a GitHub auth rewrite
(`url.https://x-access-token:...insteadOf`) so the agent can push. Diffuse's
`RepositoryMirror` verifies a mirror by comparing `git remote get-url origin`
against a credential-free clone URL; the rewrite makes that check fail and
repository indexing dies with "Existing repository mirror has an unexpected
remote" (`mirror_state: failed`). Start the app and worker with the isolated,
rewrite-free git home so they behave like production:

```bash
export HOME=/home/ubuntu/.diffuse-git-home   # empty .gitconfig, created by start.sh
```

Keep your normal shell `HOME` for your own `git commit` / `git push`.

## Startup validation and secrets

Both `service.hosted.webhook_server:app` (in its lifespan) and the worker run
`validate_worker_configuration`. Set `REVIEW_AGENT` and configure Agent
Dispatch plus Review Access Grants before starting either process; the worker
additionally checks that the isolated Agent Hosts are ready.

Onboarding and indexing a repo needs no *external* secrets, but `REVIEW_AGENT`
and the Review Access Grant signing key are startup requirements for the app
itself. Running reviews additionally needs the dispatch keys and an
authenticated Agent Host; private repos and publishing need `GITHUB_TOKEN`
plus a webhook secret. Bare-repo mirrors live under
`/var/lib/diffuse/repositories`.
