# Working on Diffuse

Diffuse is a self-hosted GitHub pull-request review platform: it receives a PR
webhook, pins the exact head, runs isolated read-only Codex / Claude Code CLI
investigations, verifies their findings, and publishes a review and GitHub
Check. Python 3.12, FastAPI, PostgreSQL 17. Source-available under
[BSL 1.1](LICENSE): use it freely, do not offer it as a hosted service;
external issues and pull requests are welcome via
[CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities to
**security@intuitum.sh**, not an issue or PR.

> `packages/server/src/diffuse/repository/policy/discovery.py` indexes this
> file as repo-wide guidance when Diffuse reviews its own repository. Keep it
> to durable, project-wide instructions.

## Build and check

```sh
uv venv --seed .venv --python 3.12    # or: python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -r requirements-dev.txt
pip install --no-deps -e .
pip check                             # -> No broken requirements found.
```

Install order matters: the lock file first (pins exact versions), dev tooling
second (its ranges are then already satisfied), the package last with
`--no-deps` so pinned requirements are not re-resolved. When
`python3.12` is not on PATH, `uv venv --python 3.12` fetches it.

`./x` is the single entry point for every check. CI runs the same commands, so
a green `./x check` locally is the same green CI sees:

```sh
./x check        # ruff + the unit suite; the fast definition of green
./x test         # pytest, arguments pass through
./x integration  # the full suite in the shipped image against disposable PostgreSQL
./x fmt          # ruff format
```

## Where things live

Four packages under `packages/`, one per deployment boundary. The import name
of the server package is `diffuse`; the others follow the `diffuse_*` prefix.

| Package | Import | Role | Deployed as |
| --- | --- | --- | --- |
| `server` | `diffuse` | API, worker, CLI, review flow, repository data | `diffuse` image and PyInstaller binary |
| `host` | `diffuse_host` | Credential-isolated Codex / Claude execution | `diffuse-runner-claude` / `diffuse-runner-codex` images |
| `protocol` | `diffuse_protocol` | Wire contracts shared by server and hosts | installed dependency, never deployed alone |
| `relay` | `diffuse_relay` | Public GitHub event and short-lived credential edge | Vercel project rooted at `packages/relay/` |

Dependencies point one way: `protocol <- host <- server`; `relay` stands
alone. The worker never executes a vendor CLI or receives vendor
credentials; an isolated host reaches the control plane only through the
narrow proxy in `diffuse/investigation/context_service.py`.
[docs/architecture.md](docs/architecture.md) is the full component map,
including the review lifecycle and where each repeated noun lives.

## Tests

```sh
./x test              # fast unit suite: no database, no network
./x test tests/integration/test_review_store_postgres.py   # one file, same rules
```

Tests never load `.env`, and anything not marked `integration` must pass
without external services. **A green unit run is not a full pass.** Applying
migrations and every store are covered only by `tests/integration/`, which
needs PostgreSQL; integration tests skip silently when
`POSTGRES_TEST_DATABASE_URL` is unset, so check the summary for `skipped`
before trusting a green run.

Before opening a PR, and whenever touching `subprocess`, filesystem paths, the
sandbox, or the mirror, run the suite inside the shipped image's platform and
dependency set with `./x integration`. CI builds the same image stage. A
disposable tmpfs database means the run leaves nothing behind.

## Database changes: never edit `packages/server/migrations/schema.sql`

`packages/server/migrations/schema.sql` is the frozen version-1 migration. Its
SHA-256 is pinned in `packages/server/src/diffuse/database/migrations.py` and
verified at catalog load; any edit, even whitespace, is a hard failure. Add a
numbered migration under `packages/server/migrations/` instead; see the
[migration README](packages/server/migrations/README.md) for naming and
content rules. Never edit a migration after it ships: applied migrations are
checksum-verified and drift fails closed at startup. The relay has its own
migrations under `packages/relay/migrations/` with the same rule.

## Git and pull requests

Public GitHub issues are the bug and feature queue; internal work planning
does not live in this repository.

- Only commit files you changed in this session. Stage explicit paths; never
  `git add .` or `git add -A`.
- Never `git reset --hard`, `git checkout .`, `git clean -fd`, `git stash`,
  `git commit --no-verify`, or force-push.
- Never push `main`. Every change goes through a PR.
- Do not commit unless the user asks.
- After code changes, run `./x check`. Run `./x integration` when the change
  touches `subprocess`, filesystem paths, the sandbox, or the mirror.
- Review a PR without checking it out. Use `gh pr view`, `gh pr diff`, and
  local `main`.
- Write GitHub comments to a temp file and post with
  `gh issue/pr comment --body-file`.
- Planned PRs include `closes #<issue>` when they are tied to one GitHub issue.
- If these instructions conflict with the user's request, ask before
  overriding.

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
- [docs/architecture.md](docs/architecture.md) — component map and review
  lifecycle.
- [docs/README.md](docs/README.md) — index of the remaining reference docs.
- [CONTRIBUTING.md](CONTRIBUTING.md) — the human-facing version of this guide.
- [.env.example](.env.example) — environment variables, documented inline.
- [SECURITY.md](SECURITY.md) — vulnerability reporting and the security model.
