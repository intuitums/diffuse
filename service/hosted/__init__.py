"""The self-hosted server surface: webhooks, queue, worker, and HTTP APIs.

This package is the process boundary that reviews pull requests from anyone who
can open one. It stays. Local `diffuse review` is a second path that can rent a
developer-installed agent CLI; it does not replace webhook ingress, the durable
job queue, REST/MCP, publication, or the one-shot API review runtime the worker
uses. Fork PRs and fleet reviews are a different threat model from a developer
driving their own authenticated CLI on a laptop.

Everything outside this package that still reaches into it:

- `workflow` -- `service.mcp_actions`, `service.repository_actions` and
  `service.cli.repository` enqueue through it, and `service.review.engine`
  imports `NonRetryableError` from it.
- `api_tokens` -- `service.api_auth` authenticates against it (and
  `service.mcp_server` authenticates through `api_auth`); `service.cli.token`
  is a front end for it.
- `analytics_store` -- `service.mcp_server` reads review analytics from it.
- `repository_mirror` -- `service.cli.repository` clones and fetches with it.
- `webhook_server` and `worker` -- `service.runtime` dispatches the packaged
  `serve` and `worker` commands to them.

`git_askpass.sh` lives here rather than in `service/` because
`repository_mirror.git_askpass_path` resolves it with `Path(__file__).with_name`
and it has no other caller.
"""
