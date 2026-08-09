# Diffuse documentation

Start with the [project README](../README.md) to install Diffuse and review a
first pull request. These pages are the reference behind it.

| Document | What it is |
| --- | --- |
| [v1-scope.md](v1-scope.md) | The active product boundary and delivery order. Read this first when a legacy plan or capability claim conflicts with current v1 decisions. |
| [agent-runtimes.md](agent-runtimes.md) | The active CLI review-runner boundary and investigation contract. |
| [capabilities.md](capabilities.md) | The concise v1 shipping ledger. |
| [architecture.md](architecture.md) | Historical system architecture. Use only for implementation archaeology until rewritten against v1 scope. |
| [roadmap.md](roadmap.md) | Historical roadmap. It is not an active delivery commitment. |
| [engineering-plan.md](engineering-plan.md) | Historical engineering plan. Use the delivery order in `v1-scope.md` instead. |
| [configuration.md](configuration.md) | The intentionally small v1 root repository configuration. |
| [deployment.md](deployment.md) | Current self-hosted deployment and Diffuse-Agent setup. |
| [infisical-hosted-production.json.example](infisical-hosted-production.json.example) | Safe placeholder-only import template for the hosted Diffuse-Agent production configuration. |
| [cli.md](cli.md) | Transitional CLI reference. Operator runner authentication remains private configuration, not a product API. |

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

The old `mcp.md` and `rest-api.md` documents were removed with their public
product surfaces; they are not supported installation options.
