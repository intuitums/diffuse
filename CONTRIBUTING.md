# Contributing

Diffuse is a self-hosted GitHub pull-request reviewer: it pins the exact PR
head, runs bounded read-only Codex and Claude Code investigations, verifies
their findings, and publishes a review and GitHub Check. It is source-available
under the [Business Source License 1.1](LICENSE): use it, modify it, and run it
anywhere, including inside a commercial organization. The one thing the license
does not permit is offering Diffuse itself to others as a hosted, managed, or
embedded service. The license converts to Apache-2.0 in 2030.

Issues and pull requests from forks are welcome. Report vulnerabilities to
**security@intuitum.sh**, never in an issue or pull request; see
[SECURITY.md](SECURITY.md).

## Getting set up

```sh
git clone https://github.com/intuitums/diffuse
cd diffuse
uv venv --seed .venv --python 3.12
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -r requirements-dev.txt
pip install --no-deps -e .
pip check
./x check
```

The lock file installs exact pinned versions; install it first, then dev
tooling, then the package itself with `--no-deps`. When `python3.12` is not on
PATH, `uv venv --python 3.12` fetches it.

## Everyday checks

`./x` is the single entry point, and CI runs the same commands:

```sh
./x check        # ruff + the unit suite; the fast definition of green
./x test         # pytest, arguments pass through
./x integration  # the full suite in the shipped image against disposable PostgreSQL
./x fmt          # ruff format
```

The unit suite needs no database or network. The integration suite needs
PostgreSQL and is covered by `./x integration`; run it before opening a PR,
and always when a change touches `subprocess`, filesystem paths, the sandbox,
or the repository mirror.

## Finding your way around

[docs/architecture.md](docs/architecture.md) is the guided tour. The short
version, one package per deployment boundary under `packages/`:

- `server` (imports as `diffuse`) — the API, worker, CLI, review flow, and
  repository data.
- `host` (`diffuse_host`) — credential-isolated Codex / Claude execution.
- `protocol` (`diffuse_protocol`) — wire contracts shared by server and hosts.
- `relay` (`diffuse_relay`) — the public GitHub event and credential edge,
  deployed to Vercel from `packages/relay/`.

Dependencies point one way: `protocol <- host <- server`; `relay` stands
alone.

## Issues and pull requests

File bugs and feature requests with the issue templates. Feature requests
should explain the need before the design; a sketch is welcome but optional.

Pull requests from forks are fine. CI runs the same `./x check` gate on fork
PRs (secrets and self-hosted runners are unavailable there, which is enough
for the unit suite). Keep the diff focused: unrelated cleanup belongs in its
own PR, even when you spotted it mid-change. Describe the change, why it is
right, and how you know it works; the template carries the checklist.

[CODEOWNERS](.github/CODEOWNERS) marks the trust boundary: the agent host and
sandbox, the policy engine, the container build, deployment, and CI. PRs that
touch those paths need the maintainer's review and cannot merge on green
checks alone.

Substantial changes go better with an issue first, so the design gets
discussed before the code lands.

## House rules

- Add or update tests for behavior that changes. A regression test should
  fail against the unfixed code, for the intended reason.
- Update comments and docs your change touches. A stale comment is worse
  than none.
- Database changes are numbered migrations under `packages/server/migrations/`;
  the frozen `schema.sql` and every shipped migration are checksum-verified
  and editing them in place fails closed at startup.
- The product boundary is [docs/v1-scope.md](docs/v1-scope.md). Read it first
  whenever an older document or code comment conflicts with it.

Contributions are accepted under the Business Source License 1.1, without a
separate contributor agreement. Submitting a PR says the work may ship under
that license.
