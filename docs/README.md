# Diffuse documentation

Start with the [project README](../README.md) to install Diffuse and review a
first pull request. These pages are the reference behind it.

| Document | What it is |
| --- | --- |
| [capabilities.md](capabilities.md) | The acceptance ledger: every required capability and its honest status. The authoritative answer to "does Diffuse do X yet?" |
| [architecture.md](architecture.md) | Target architecture, component boundaries, data model, durable workflow model, and security invariants. Says explicitly where it describes a target rather than the current state. |
| [roadmap.md](roadmap.md) | Delivery phases, ordered by dependency and risk, with the exit condition each phase has to pass. |
| [engineering-plan.md](engineering-plan.md) | The near-term work in dependency order, with per-item status and the open questions that gate it. Shorter-lived than the roadmap. |
| [agent-runtimes.md](agent-runtimes.md) | CLI-native runtime plan: control plane vs isolated agent-runner, transitional LiteLLM path, and what has landed. |
| [configuration.md](configuration.md) | Repository review policy: every `.diffuse/` file, field, default, and inheritance rule. |
| [deployment.md](deployment.md) | Single-server operation: host preparation, secrets, backups and restore drills, upgrades, rollback, and the operational checklist. |
| [cli.md](cli.md) | The `diffuse` command: every subcommand, the local-review flags and output modes, and the stable exit-code table. |
| [mcp.md](mcp.md) | The MCP server at `/mcp`: authentication, scopes, the twenty-two tools, fix handoffs, and analytics. |
| [rest-api.md](rest-api.md) | The versioned REST API under `/api/v1`: scopes, conventions, and idempotency. The schema itself is served at `/openapi.json`. |

Elsewhere in the repository:

- [`DEVELOPMENT.md`](../DEVELOPMENT.md) — environment setup, tests, the
  review-quality harness, migration rules, and the dependency workflow.
- [`SECURITY.md`](../SECURITY.md) — how to report a vulnerability.
- [`evals/README.md`](../evals/README.md) — the evaluation scorer, the fixture
  harness, and what a captured baseline records.
- [`.env.example`](../.env.example) — every environment variable, documented
  inline at its definition.
- [`deploy/README.md`](../deploy/README.md) — installing a tagged release from
  published, signature-verified images.
