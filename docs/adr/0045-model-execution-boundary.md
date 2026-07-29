# ADR 0045: Model execution is a constrained Diffuse subsystem

- Status: Accepted
- Date: 2026-07-29

## Context

Diffuse originally called LiteLLM directly from a private helper shared by
review, conversation, code-query, and learning workloads. A review could make
many model calls, while PostgreSQL stored only the final report. One late
failure therefore reran every earlier call.

Supporting an authenticated Codex or Claude CLI by adding subprocess branches
to that helper would also give an agent-oriented program an unnecessarily
large and poorly specified trust surface. Diffuse—not the CLI—owns repository
authorization, retrieval, policy, validation, persistence, and publication.

## Decision

Diffuse separates three capabilities:

1. `StructuredGenerator` produces one schema-constrained value from
   Diffuse-prepared instructions and untrusted content.
2. `Embedder` indexes and retrieves repository context.
3. A future `WorkspaceAgent` may receive a checkout and tools for an explicit
   fix workflow. It is not used for review generation.

Every review has an immutable execution plan containing executor, requested
models, structured-output mode, and runner protocol version. Its fingerprint
is part of review identity.

Candidate pass/chunk calls, the optional diagram, and the verifier are durable
generation steps. A step is reusable only when its exact prompt, schema,
target, and output limit fingerprint matches and its stored response still
passes Pydantic validation.

CLI execution crosses a versioned, permissioned Unix-socket protocol. The
request has no working-directory, environment, executable, arguments, tools,
MCP, or publication fields. It declares:

- `content_profile=diffuse_prepared_content_v1`;
- `workspace_access=none`;
- `publication_authority=false`.

The host runner owns executable selection and flags. It runs each child in an
empty temporary directory with an allowlisted environment and a bounded
deadline/output budget. Provider keys and Diffuse, database, relay, and GitHub
credentials are not inherited.

The shared Diffuse App remains the only publisher. A CLI can return structured
review data but cannot select a repository, fetch a pull request, post a
comment, or call the hosted relay as Diffuse.

There is no implicit fallback between CLI and provider-API execution.

## Consequences

- Provider APIs remain compatible through the `litellm` adapter.
- The worker can resume completed model stages after a transient failure.
- CLI adapters can be implemented and tested without changing review
  orchestration or publication authority.
- CLI version/authentication failures can be normalized at one boundary.
- Ordinary reviews do not gain repository tools merely because an
  agent-oriented CLI is selected.
- Local embeddings remain a separate requirement.

The production Codex and Claude adapters implement those gates at runner
startup. An adapter is advertised only after executable, version/flag
capability, and authentication probes succeed. Each invocation additionally
enforces the adapter-specific isolation flags, native JSON Schema output,
deadline/output bounds, second validation in the worker, and request-scoped
process-group cancellation. The deterministic fake backend remains available
only behind an explicit test flag.
