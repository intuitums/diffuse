# CLI review runners

> **Status: active v1 direction.** This page replaces the old “agent runtime”
> plan. When code or an older document conflicts with it, follow
> [v1-scope.md](v1-scope.md).

Diffuse owns GitHub webhook ingestion, commit-pinned context, orchestration,
verification, lineage, and publication. Codex and Claude Code are isolated,
read-only **CLI review runners**. They investigate one exact pull-request head
and return structured candidate findings; they never publish to GitHub or hold
Diffuse control-plane credentials.

## Boundary

```text
Diffuse worker
  -> private review transport
  -> isolated Codex or Claude Code CLI review runner
  -> structured candidate findings
  -> Diffuse verifier + deterministic publication
```

The worker never executes a vendor CLI and never mounts a vendor credential.
Each runner has only its own credential home, a read-only review workspace,
bounded vendor egress, and private access to the exact immutable context it was
issued. It has no database, GitHub App, operator API token, branch-write, or
arbitrary-network access.

The transport may use a narrowly scoped internal protocol, but it is not a
public MCP server and is not a product integration surface. It must expose only
the context operations required for a review investigation.

## Current pilot containment

The source-workspace Compose profile has opt-in `agent-runner-codex` and
`agent-runner-claude` services. They use independently built runner images and
separate credential volumes; the application and worker services mount neither
credential volume. Build the local images before enabling a runner profile:

```bash
docker build --target runner-codex --tag diffuse-runner-codex:local .
docker build --target runner-claude --tag diffuse-runner-claude:local .
docker compose --profile agent-codex --profile agent-claude up -d --build
```

For each session, the worker produces a bounded source artifact from the exact
head it already checked out. The runner verifies and materializes it into an
empty read-only workspace; it never receives SCM credentials, clones a
repository, or mounts a repository mirror. The current pilot bounds the source
archive to 16 MiB and the extracted workspace to 128 MiB.

Operator sign-in happens only in the corresponding runner context. It is not a
Diffuse browser-login flow:

```bash
docker compose --profile agent-codex run --rm agent-runner-codex agent login codex --device-auth
docker compose --profile agent-claude run --rm agent-runner-claude agent login claude --console
```

## Investigation contract

An investigation is bound to one repository, pull request, index snapshot, and
head SHA. It has a strict timeout, bounded work budget, role, and read-only
operation allowlist.

Its output is a **candidate**, not a final review. Every candidate must name:

- the exact location and code evidence;
- severity and category;
- why the change fails or is risky; and
- the relevant head SHA and investigation identity.

An independent verifier can reject candidates. Diffuse performs final
exact-head validation, deduplication, lineage changes, and GitHub publication.

## Delivery sequence

1. Keep the runner isolation boundary and private, exact-head context access.
2. Rename old agent/session/runtime terminology in code and configuration to
   review-runner/investigation/engine terminology.
3. Complete one end-to-end Codex review engine, including verification and
   publication. Add Claude through the same contract.
4. After evidence from the single-engine path, add the bounded correctness,
   security, and integration team plus a verifier.

## Transitional implementation

The current `litellm` path exists only as a migration aid. It is not v1’s
product identity and does not justify retaining provider-routing, public MCP,
conversation, Q&A, or policy-engine features. Whether to retire it is decided
only after the CLI review path has pilot evidence.

Current code still uses legacy names such as `REVIEW_RUNTIME`, “agent runner,”
and “session capability.” These are migration seams, not public terminology.
The supported v1 configuration name is `REVIEW_ENGINE`; implementation changes
must preserve the isolation boundary while moving to that contract.
