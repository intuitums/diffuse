# ADR 0026: Immutable source search and grounded code Q&A

Date: 2026-07-23

Status: Accepted

## Context

Greptile publicly describes a whole-codebase graph that retrieves related code
and supports explanations and follow-up questions. Diffuse already used its
graph, lexical index, and vectors for reviews, but its retrieval entry point
expected a pull-request diff. Re-encoding general questions as fake diffs would
mix two contracts, bias path/line logic, and make source provenance harder to
verify.

Repository questions also cross two trust boundaries. Code and questions can
contain prompt injection, and a repository-scoped token must not gain access to
every repository in an operator-managed cluster. Free-form model prose with
decorative links is not adequate grounding.

Public references:

- <https://www.greptile.com/docs/developer-quick-reference>
- <https://www.greptile.com/docs/code-review/developer-essentials>
- <https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context>

## Decision

- Add a first-class arbitrary-text retrieval path instead of manufacturing a
  pull-request diff. It extracts bounded lexical terms, embeds one bounded
  query, searches exact snapshot IDs, and expands the strongest lexical/vector
  seeds through one-hop graph relationships.
- Resolve `name`, `remote`, `defaultBranch`, and optional `remoteUrl` under the
  authenticated token's repository claims. Freeze the active compatible
  primary snapshot before retrieval. A concurrent index activation cannot
  change the in-flight result.
- Make cross-repository search opt-in. Include only operator-cluster snapshots
  that are also present in the token's repository grants; never reveal omitted
  cluster members.
- Treat `path` as a normalized literal repository-relative prefix. Escape SQL
  wildcard characters and apply the scope to lexical, vector, and graph-result
  queries.
- Add read-scoped `search_code`, returning bounded source excerpts, exact
  snapshot/commit identity, retrieval scores/reasons, and GitHub/GitLab commit
  permalinks.
- Add `ask_codebase` behind a dedicated `diffuse:mcp:generate` scope (or
  administrative/bootstrap authority). Bound its source context, output tokens,
  and model timeout independently from review generation.
- Require structured claim-level citations. Retain a claim only if every cited
  repository/path/range maps unambiguously inside the exact evidence sent to
  the model. Drop invalid claims as a unit; return a fixed
  insufficient-evidence response when none survive.
- Treat questions and source as untrusted data. The prompt forbids following
  embedded instructions, exposing secrets, claiming execution, or taking
  external actions. Return model, prompt, token, snapshot-plan, and query/source
  fingerprint provenance.

## Consequences

MCP clients can inspect and ask questions about self-hosted repositories without
depending on mutable branches or accepting invented source links. Plain search
does not spend generation capacity, and a read-only token cannot invoke Q&A.
Repository cluster configuration never overrides token authorization.

Mechanical citation validation proves that a cited range was supplied; it
cannot prove that every natural-language inference is semantically correct.
Claim-structured output, fail-closed filtering, and visible source excerpts make
that limitation inspectable. Multi-hop/type-aware retrieval, durable
generation quotas/rate accounting, public API/UI surfaces, and dedicated
retrieval/answer evaluation remain future work.
