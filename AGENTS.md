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
`README.md`, `DEVELOPMENT.md`, `docker-compose.yml`, `.github/workflows/verify.yml`, and
`pyproject.toml`. This section only records the non-obvious, environment-specific things a
cloud agent needs.

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

### `.env` no longer leaks into the test process

`litellm` calls `load_dotenv()` on import, so importing any service module auto-loads `.env`
from the current directory into the process environment. That used to break otherwise-passing
tests whenever a developer had followed the setup instructions and created one — the dev
`DIFFUSE_MCP_ALLOWED_HOSTS`, for instance, rejects the `testserver` host the MCP test uses.

`tests/conftest.py` now sets `LITELLM_MODE=PRODUCTION` before the first litellm import, which
disables that load for the test process only. Running tests with `.env` in place is fine; the
old `mv .env .env.bak` dance is no longer needed. The app and worker still load `.env`
normally, which is what you want for local development.

That guard only stops litellm from auto-loading the on-disk `.env`; it cannot undo variables
you export yourself. So do **not** run `pytest` from a shell where you have already done
`set -a; source .env` (which is how you launch the app/worker below). Those exports put
`DIFFUSE_MCP_ALLOWED_HOSTS` into the environment, `pytest` inherits it, and the same MCP test
fails with `421 Misdirected Request` for `http://testserver/mcp` — a failure that looks like a
code bug but is pure shell pollution. Run the app in one shell and the tests in a separate,
un-sourced shell.

```bash
.venv/bin/python -m pytest -m "not integration"
POSTGRES_TEST_DATABASE_URL=postgresql://diffuse:diffuse-dev@127.0.0.1:5432/diffuse_test \
  .venv/bin/python -m pytest -m integration            # migrate the test DB first
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

### Both the app and the worker validate config at startup — `.env` ships placeholders

`service.webhook_server:app` runs `validate_worker_configuration` in its lifespan, so the API
server — not just the worker — refuses to boot unless `REVIEW_MODEL` is set and an embedding
credential is present. That check is offline: it confirms the model identifier and a resolvable
credential *name*, and never calls the provider. A placeholder key therefore satisfies startup;
only real indexing/review calls fail on a bad one. The committed CI smoke test (`verify.yml`
`build-container`) relies on exactly this, booting with `REVIEW_MODEL=anthropic/claude-sonnet-5`
and a fake `OPENAI_API_KEY`.

The dev `.env` on this VM is set up the same way: `REVIEW_MODEL=anthropic/claude-sonnet-5` plus
placeholder `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, so the app and worker boot and the whole
control plane (onboarding, CLI, REST `/api/v1`, MCP, `/health`, `/ready`) works. Replace those
placeholders with real keys before expecting indexing or a review to complete.

### External secrets for full end-to-end review

Onboarding a repo (clone + resolve exact commit + durable queue) works with no external secrets,
and the worker indexes up to the embedding call. Full indexing/review additionally needs:

- `OPENAI_API_KEY` (or `REVIEW_API_BASE`) — embeddings + review model. With only the placeholder
 above, a leased index job fails at the embedding call with a 401 (`EmbeddingCredentialError`),
 which is expected; the worker stays up and retries with backoff.
- `GITHUB_TOKEN` + webhook secret — to clone private repos and publish reviews. Public repos
 clone with no token (verified by onboarding `octocat/Hello-World` to `mirrorState: ready`).

The bare-repo mirrors live under `/var/lib/diffuse/repositories` (created, owned by `ubuntu`).
