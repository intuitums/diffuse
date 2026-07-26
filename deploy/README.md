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

Read `OPERATIONS.md` before onboarding production repositories. Back up
PostgreSQL before every upgrade. Never share the registry credential, `.env`,
SCM credentials, or model credentials with Diffuse support.
