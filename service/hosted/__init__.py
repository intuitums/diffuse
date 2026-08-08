"""The self-hosted server surface: webhooks, queue, worker, and private transport.

This package is the process boundary that reviews pull requests from anyone who
can open one. It stays. Local `diffuse review` is a second path that can rent a
developer-installed agent CLI; it does not replace webhook ingress, the durable
job queue, publication, or the one-shot API review runtime the worker uses.
Fork PRs and fleet reviews are a different threat model from a developer
driving their own authenticated CLI on a laptop. Public MCP and REST are not
part of this package's v1 surface.

Everything outside this package that still reaches into it:

- `workflow` -- `service.cli.repository` enqueues through it, and
  `service.review.engine` imports `NonRetryableError` from it.
- `repository_mirror` -- `service.cli.repository` clones and fetches with it.
- `webhook_server` and `worker` -- `service.runtime` dispatches the packaged
  `serve` and `worker` commands to them.

`git_askpass.sh` lives here rather than in `service/` because
`repository_mirror.git_askpass_path` resolves it with `Path(__file__).with_name`
and it has no other caller.
"""
