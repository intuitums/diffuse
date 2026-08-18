# Diffuse documentation

Start with the [project README](../README.md) to install Diffuse and review a
first pull request. These pages are the reference behind it.

| Document | What it is |
| --- | --- |
| [v1-scope.md](v1-scope.md) | The active product boundary and delivery order. Read this first when a legacy plan or capability claim conflicts with current v1 decisions. |
| [agents.md](agents.md) | The active CLI review-runner boundary and investigation contract. |
| [capabilities.md](capabilities.md) | The concise v1 shipping ledger. |
| [configuration.md](configuration.md) | The intentionally small v1 root repository configuration. |
| [deployment.md](deployment.md) | Current self-hosted deployment and Diffuse GitHub App connection. |
| [infisical-hosted-production.json.example](infisical-hosted-production.json.example) | Safe placeholder-only import template for GitHub Integration Service production configuration. |
| [cli.md](cli.md) | Transitional CLI reference. Operator runner authentication remains private configuration, not a product API. |

Elsewhere in the repository:

- [`AGENTS.md`](../AGENTS.md) — environment setup, tests, migration
  rules, and the dependency workflow.
- [`SECURITY.md`](../SECURITY.md) — how to report a vulnerability.
- [`.env.example`](../.env.example) — the deployment environment variables,
  documented inline at their definitions. Some operator-tunable worker and
  review knobs exist beyond this file; the code that reads them is
  authoritative.
- [`deploy/README.md`](../deploy/README.md) — installing a tagged release from
  published, signature-verified images.
- [`packages/relay/README.md`](../packages/relay/README.md) — operating the
  small public event and credential relay.

The old `mcp.md` and `rest-api.md` documents were removed with their public
product surfaces; they are not supported installation options.
