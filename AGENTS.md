# AGENTS.md

> **Scope: this file describes ONE environment — the Cursor Cloud VM.** Almost nothing
> in it generalises: there is no Docker, PostgreSQL runs natively, and the paths and
> credentials are that VM's. If you are not on that VM, read `DEVELOPMENT.md` instead.
>
> Note also that `repository_policy/discovery.py` indexes `AGENTS.md` as scoped review
> guidance, so this file is fed into Diffuse's reviews of Diffuse. Keep it free of
> anything that reads as a project-wide instruction.

## Cursor Cloud specific instructions

Diffuse is a self-hostable code-review platform. Standard setup/run/test commands live in
`README.md`, `DEVELOPMENT.md`, `docker-compose.yml`, `.github/workflows/ci.yml`, and
`pyproject.toml`. This section only records the non-obvious, environment-specific things a
cloud agent needs.

### Services (run natively, not via Docker)

This VM has no Docker. The dev stack runs natively:

- **PostgreSQL 17** — native cluster `17/main` on `127.0.0.1:5432`. Role `diffuse`
  (password `diffuse-dev`, granted `SUPERUSER`), databases `diffuse` (dev) and `diffuse_test`
  (integration tests). Nothing uses pgvector any more, but the frozen version-1 baseline still
  runs `CREATE EXTENSION IF NOT EXISTS vector` before migration 0010 drops it, so the extension
  must stay installed on the server. The cluster is NOT auto-started on a
  fresh pod boot (no systemd); start it with `sudo pg_ctlcluster 17 main start` (check with
  `pg_lsclusters`).
- **API/app** — `uvicorn service.hosted.webhook_server:app` on `127.0.0.1:8000` (serves
  GitHub webhooks, private runner transport, `/health`, and `/ready`). Single FastAPI process.
- **worker** — `python -m service.hosted.worker` (leases jobs from the Postgres queue; there is no
  separate broker/cache).

Python dependencies live in `.venv` (created by the startup update script). Use `.venv/bin/...`
or activate it. `.env` (gitignored) already exists with dev values and points `DATABASE_URL` at
the native localhost Postgres.

### `.env` no longer leaks into the test process

Tests do not load `.env` automatically. Running tests with `.env` in place is fine, but do
**not** run `pytest` from a shell where you have already done
`set -a; source .env` (which is how you launch the app/worker below). Run the app in one shell
and tests in a separate, un-sourced shell.

```bash
.venv/bin/python -m pytest -m "not integration"
POSTGRES_TEST_DATABASE_URL=postgresql://diffuse:diffuse-dev@127.0.0.1:5432/diffuse_test \
  .venv/bin/python -m pytest -m integration            # migrate the test DB first
```

Integration tests migrate databases from scratch, and the frozen version-1 baseline still
creates the `vector` extension (migration 0010 drops it again), which requires the `diffuse`
role to be a Postgres `SUPERUSER` (already granted here; the CI/Docker `diffuse` user is a
superuser too).

### GOTCHA 2 — run the app + worker with an isolated git `HOME`

The VM's global `~/.gitconfig` contains a GitHub auth rewrite
(`url.https://x-access-token:<token>@github.com/.insteadOf = https://github.com/`) so the agent
can push. Diffuse's `RepositoryMirror` verifies a mirror by comparing `git remote get-url origin`
against a credential-free clone URL; the global `insteadOf` rewrite makes `get-url` return a
credentialed URL, so the equality check fails and repository indexing dies with
"Existing repository mirror has an unexpected remote" (`mirrorState: failed`). Production service
users have no such rewrite. Start the app and worker with an isolated, rewrite-free git home so
they behave like production:

```bash
export HOME=/home/ubuntu/.diffuse-git-home   # empty .gitconfig, already created
```

With that set, `mirrorState` reaches `ready` and the worker clones/fetches/checks out commits
normally. (This only affects Diffuse's git subprocesses; keep your normal shell `HOME` for your
own `git commit`/`git push`.)

### Both the app and worker validate Agent configuration at startup

`service.webhook_server:app` runs `validate_worker_configuration` in its lifespan. Set
`REVIEW_AGENT` and configure Agent Dispatch plus Review Access Grants before starting either
process. The worker also checks that the isolated Agent Hosts are ready.

### External secrets for full end-to-end review

Onboarding *and indexing* a repo works with no external secrets: retrieval is graph and
lexical search inside PostgreSQL. Review additionally needs:

- `REVIEW_AGENT`, Agent Dispatch signing, a Review Access Grant signing key, and an authenticated
  isolated Agent Host.
- `GITHUB_TOKEN` + webhook secret — to clone private repos and publish reviews. Public repos
  clone with no token (verified by onboarding `octocat/Hello-World` to `mirrorState: ready`).

The bare-repo mirrors live under `/var/lib/diffuse/repositories` (created, owned by `ubuntu`).
