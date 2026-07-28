# Diffuse self-hosted release

This bundle installs a proprietary Diffuse release without access to the
private source repository. It contains the deployment manifest and operational
documentation, not the application source.

## Install

1. Obtain read access to the private `ghcr.io/intuitumxyz/diffuse` package and
   authenticate Docker using the customer credential supplied by Diffuse.
2. Copy `env.example` to `.env`, restrict it with `chmod 600 .env`, and fill
   every required value. Release bundles already pin `DIFFUSE_IMAGE` to the
   immutable release digest.
3. Verify the image signature before starting it:

   ```bash
   image_ref="$(sed -n 's/^DIFFUSE_IMAGE=//p' .env)"
   cosign verify \
     --certificate-identity-regexp \
       '^https://github.com/intuitumxyz/Diffuse/.github/workflows/release.yml@refs/tags/v' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     "$image_ref"
   ```

4. Pull and start the migration-gated stack:

   ```bash
   docker compose --env-file .env pull
   docker compose --env-file .env up -d
   docker compose ps
   curl --fail http://127.0.0.1:8000/ready
   ```

5. Create the GitHub webhook, pointing it at
   `https://your-diffuse-host/webhook/github` with the `GITHUB_WEBHOOK_SECRET`
   from `.env`. Enable exactly these four event types and no others:

   - `push`
   - `pull_request`
   - `issue_comment`
   - `pull_request_review_comment`

6. Onboard each repository you want reviewed. **Nothing is reviewed until you
   do this**, and a webhook for a repository that was never onboarded is
   refused with HTTP 409 — which reads exactly like the webhook not being
   delivered at all. See the "When nothing appears to happen" section of
   `OPERATIONS.md` for how to tell the two apart.

   ```bash
   docker compose --env-file .env run --rm worker repository add \
     --provider github \
     --base-url https://github.com \
     --repo owner/name \
     --default-branch main

   docker compose --env-file .env run --rm worker repository list
   ```

   `repository list` reports `mirror_state` per repository. Wait for the
   initial index to finish before expecting a review: a repository whose index
   has not been built yet cannot be reviewed.

7. Open a pull request to confirm the path end to end. Note that by default
   Diffuse reviews a pull request when it opens but **not** when you push
   further commits to it, and publishes no status check. Both are opt-in per
   repository through `.diffuse/config.json`; see `CONFIGURATION.md`, included
   in this bundle.

## Running Diffuse commands

There is no `diffuse` binary to install: every command runs inside the release
image. Anywhere the documentation shows `diffuse <command>`, the Compose form is:

```bash
docker compose --env-file .env run --rm worker <command>
```

For example, `diffuse token add` becomes
`docker compose --env-file .env run --rm worker token add`. The available
subcommands are `review`, `repository`, `cluster`, `learning`, `token`,
`database`, `evaluate`, and `model`; each accepts `--help`.

## Obtaining this bundle and later ones

Every release publishes this bundle as an OCI artifact in the same private
registry as the image, so it needs the credential you already have and no access
to the Diffuse source repository:

```bash
oras pull ghcr.io/intuitumxyz/diffuse-self-host:vX.Y.Z
sha256sum --check diffuse-self-host.tar.gz.sha256
tar -xzf diffuse-self-host.tar.gz
```

Ask Diffuse for read access to the `diffuse-self-host` package alongside the
`diffuse` package. Artifacts are immutable and are not garbage collected, so an
older release stays retrievable at its own tag; the `DIFFUSE_IMAGE` digest each
bundle pins is also recorded in the artifact's
`xyz.intuitum.diffuse.image` annotation:

```bash
oras manifest fetch --pretty ghcr.io/intuitumxyz/diffuse-self-host:vX.Y.Z
```

Upgrade by pulling the new bundle, re-running the signature verification above
against its `DIFFUSE_IMAGE`, and following `OPERATIONS.md`. Carry your existing
`.env` values across rather than editing the new `env.example` in place — note in
particular that `POSTGRES_PASSWORD` cannot be changed by editing the file once the
database volume exists.

## Support

Read `OPERATIONS.md`, included alongside this file in the release bundle, before
onboarding production repositories. (It is not present in the source
repository — the release build generates it from `docs/deployment.md`.) Back up
PostgreSQL before every upgrade. Never share the registry credential, `.env`,
SCM credentials, or model credentials with Diffuse support.
