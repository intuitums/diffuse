# Diffuse control room

The control room is the optional managed-cloud surface for Diffuse. It uses
Next.js for the operator UI and Convex for bounded, reactive status
projections. PostgreSQL remains the authoritative data plane in both
self-hosted and managed installations.

Convex may contain:

- deployment health and version;
- repository identity, index state, and aggregate finding counts;
- review-run status, latency, and aggregate finding counts;
- evaluation precision, recall, F1, latency, and cost; and
- model name/readiness (never a provider credential).

Convex must not contain source, diffs, embeddings, prompts, model evidence,
finding bodies, clone credentials, SCM tokens, or provider API keys.

## Local development

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm lint
pnpm typecheck
pnpm build
```

Set up a Convex development deployment with `pnpm convex:check`. An operator
may install the non-sensitive preview with
`convex run controlPlane:bootstrapDemo`; it is an internal function and cannot
be invoked from a browser. Production data planes publish to
`/v1/data-plane/snapshot` using an HMAC signature and a five-minute replay
window.

The Convex deployment needs `DIFFUSE_CONTROL_PLANE_SIGNING_KEY`. Each data
plane receives the same value through a mode-600 secret file, referenced by
`DIFFUSE_CONTROL_PLANE_SIGNING_SECRET_FILE`; do not place it in the repository.

No production deployment is performed by the repository scripts or CI.
