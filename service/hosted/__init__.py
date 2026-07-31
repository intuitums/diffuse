"""The hosted-service surface, scheduled for deletion.

Diffuse is pivoting from a hosted service to a CLI, and nothing in this package
survives that: the REST API, the webhook ingress, the durable job queue and its
worker, the OAuth login flow, the API-token and idempotency tables, the
analytics rollups, and the server-side repository mirror. They stay only because
the CLI has not yet absorbed what they do. Grouping them here means the deletion
is `git rm -r service/hosted` plus the edges below, rather than an archaeology
exercise across a flat directory.

Everything outside this package that still reaches into it, and therefore has to
be resolved before the directory can go:

- `workflow` -- `service.mcp_actions`, `service.repository_actions` and
  `service.cli.repository` enqueue through it, and `service.review.engine`
  imports `NonRetryableError` from it.
- `api_tokens` -- `service.api_auth` authenticates against it (and
  `service.mcp_server` authenticates through `api_auth`); `service.cli.token`
  is a front end for it and goes with it.
- `analytics_store` -- `service.mcp_server` reads review analytics from it.
- `repository_mirror` -- `service.cli.repository` clones and fetches with it.
- `webhook_server` and `worker` -- `service.runtime` dispatches the packaged
  `serve` and `worker` commands to them.

`git_askpass.sh` lives here rather than in `service/` because
`repository_mirror.git_askpass_path` resolves it with `Path(__file__).with_name`
and it has no other caller.
"""
