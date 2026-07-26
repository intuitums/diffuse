# AGENTS.md

## Cursor Cloud specific instructions

Diffuse is a self-hostable code-review platform. Standard setup/run/test commands live in
`README.md`, `docker-compose.yml`, `.github/workflows/ci.yml`, and `pyproject.toml`. This
section only records the non-obvious, environment-specific things a cloud agent needs.

### Services (run natively, not via Docker)

This VM has no Docker. The dev stack runs natively:

- **PostgreSQL 17 + pgvector** — native cluster `17/main` on `127.0.0.1:5432`. Role `diffuse`
  (password `diffuse-dev`, granted `SUPERUSER`), databases `diffuse` (dev) and `diffuse_test`
  (integration tests), both with the `vector` extension. The cluster is NOT auto-started on a
  fresh pod boot (no systemd); start it with `sudo pg_ctlcluster 17 main start` (check with
  `pg_lsclusters`).
- **API/app** — `uvicorn service.webhook_server:app` on `127.0.0.1:8000` (serves REST `/api/v1`,
  webhooks, MCP `/mcp`, `/docs`, `/health`, `/ready`). Single FastAPI process.
- **worker** — `python -m service.worker` (leases jobs from the Postgres queue; there is no
  separate broker/cache).

Python dependencies live in `.venv` (created by the startup update script). Use `.venv/bin/...`
or activate it. `.env` (gitignored) already exists with dev values and points `DATABASE_URL` at
the native localhost Postgres.

### GOTCHA 1 — run `pytest` without `.env` in the working directory

`litellm` calls `load_dotenv()` on import, so importing any service module auto-loads `.env` from
the current directory into the process environment. The dev `.env` values then leak into the test
process and break 2 otherwise-passing tests (e.g. `DIFFUSE_MCP_ALLOWED_HOSTS` rejects the
`testserver` host used by the MCP test). CI has no `.env`, so it is unaffected. When running tests
locally, move `.env` aside first, for example:

```bash
mv .env .env.bak
.venv/bin/python -m pytest -m "not integration"        # 483 pass
POSTGRES_TEST_DATABASE_URL=postgresql://diffuse:diffuse-dev@127.0.0.1:5432/diffuse_test \
  .venv/bin/python -m pytest -m integration            # 43 pass (migrate test DB first)
mv .env.bak .env
```

Integration tests re-create the `vector` extension from scratch, which requires the `diffuse`
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

### External secrets for full end-to-end review

Onboarding a repo (clone + resolve exact commit + durable queue) works with no external secrets,
and the worker indexes up to the embedding call. Full indexing/review additionally needs:

- `OPENAI_API_KEY` (or `REVIEW_API_BASE`) — embeddings + review model. Without it the worker
  reaches `indexer/embed.py` and fails with "Missing credentials … OPENAI_API_KEY".
- `GITHUB_TOKEN` / `GITLAB_TOKEN` + webhook secrets — to clone private repos and publish reviews.

The bare-repo mirrors live under `/var/lib/diffuse/repositories` (created, owned by `ubuntu`).
