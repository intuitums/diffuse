"""Webhook, workflow, and native review services for Diffuse.

The subpackages here are named after the filename families they replace, so an
old module name still predicts its location:

- `cli/` -- the `*_cli` argparse subcommands (`review_cli` -> `cli.review`).
- `storage/` -- the `*_store` PostgreSQL adapters (`review_store` ->
  `storage.review`), plus `storage.migrations`.
- `models/` -- the `*_models` domain types (`review_models` -> `models.review`).
- `github/` -- `github.py` and the `github_*` publication surfaces
  (`github.py` -> `github.api`, `github_review` -> `github.review`).
- `review/` -- the remaining `review_*` run machinery (`review_engine` ->
  `review.engine`).
- `hosted/` -- the self-hosted server surface (webhooks, queue, worker, HTTP).
  Names are unchanged in there; see its docstring.

Where a name was in two families the layer suffix won over the topic prefix,
which is why `review_cli`, `review_store` and `review_models` are in `cli/`,
`storage/` and `models/` rather than in `review/`. The modules left at this
level belong to no family: they are the cross-cutting leaves (`scm`,
`runtime`, `diff_parser`, `model_providers`, ...).
"""
