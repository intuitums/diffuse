# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Report privately through GitHub's private vulnerability reporting:

1. Go to <https://github.com/intuitumxyz/diffuse/security/advisories/new>, or
   open the repository's **Security** tab and choose **Report a vulnerability**.
2. Include the affected version or commit SHA, the deployment shape
   (Docker Compose, other), the provider involved (GitHub, GitLab, or neither),
   reproduction steps or a proof of concept, and the impact you believe it has.

This creates a private advisory visible only to you and the maintainers.

If GitHub private reporting is unavailable to you, open a public issue that
contains **no** technical detail — only a request for a private channel — and a
maintainer will follow up.

## What to expect

- Acknowledgement of your report within 5 business days.
- An initial assessment, including whether we consider it in scope, within
  10 business days.
- Progress updates until the issue is resolved or closed, and credit in the
  published advisory unless you ask us not to.

Diffuse has no paid bug bounty. Please give us a reasonable opportunity to ship
a fix before disclosing publicly.

## Scope

Diffuse is self-hosted: operators run it inside their own environment, against
their own repositories, with their own credentials. Reports are most useful when
they describe something an operator cannot simply configure away.

### Diffuse processes untrusted repository content

This is the defining property of the threat model. Diffuse clones repositories,
indexes their source, reads in-repository configuration (`.diffuse`,
`greptile.json`, agent/editor instruction files), ingests webhook payloads, and
feeds diffs, retrieved code, and pull-request and merge-request text to a
language model. **All of that content is attacker-influencable** — by an
external contributor opening a pull request, by a compromised dependency
vendored into a repository, or by anyone who can get a branch pushed.

The following are explicitly **in scope**:

- **Prompt injection.** Repository content, diffs, PR/MR titles and
  descriptions, commit messages, review threads, or in-repository configuration
  that steers the review model into leaking data it was given, taking an
  unintended action (publishing, approving, re-triggering), suppressing
  findings for other changes, or escaping the constraints of the review passes.
- **Sandbox and isolation escape.** Anything in repository content, a crafted
  filename, a symlink, a Git attribute, submodule, hook, or LFS pointer that
  causes code execution, filesystem access, or network access outside the
  disposable worktree or mirror — including escaping the repository root,
  bypassing `DIFFUSE_MAX_REPOSITORY_BYTES`, or reaching the host from a
  container.
- **Credential exposure.** Any path that leaks `GITHUB_TOKEN`, `GITLAB_TOKEN`,
  the OAuth client secret, `DIFFUSE_API_TOKEN`, or a repository-scoped service
  token into a clone URL sent to an unintended origin, into model input, into
  published review output, into logs, or into an image layer.
- **Authorization bypass.** Reading or writing another repository's index,
  findings, analytics, or custom context across a repository-scoped token
  boundary; escalating a read scope to a generation or write scope; acting on a
  repository the token is not assigned to.
- **Webhook authentication flaws.** Signature-verification bypass, replay
  outside the bounded window, downgrade from Standard Webhooks signatures to the
  legacy token, or forging an instance origin.
- **OAuth and session flaws** in the browser sign-in path (`/auth/cli`,
  `/auth/github/callback`, `/setup`), including state fixation or reuse,
  session-token exposure, and open redirects.
- **MCP and REST API flaws**, including DNS-rebinding protection bypass and
  authentication bypass.
- **Database migration integrity** failures that allow unverified SQL to be
  applied.

### Out of scope

- Vulnerabilities in the language model provider itself, or model output that is
  merely low quality, wrong, or unhelpful without a security consequence.
- Findings that require an operator to have already set a documented-unsafe
  configuration, for example `DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1` off loopback,
  or granting a provider token more permission than the README asks for.
- Vulnerabilities in third-party dependencies with no exploitable path in
  Diffuse. Report those upstream; CI already runs `pip-audit` against
  `requirements.lock`.
- Missing hardening that is the operator's responsibility in a self-hosted
  deployment: TLS termination, network placement, host patching, secret-manager
  choice, or database backups.
- Denial of service from unrealistic request volume against an instance the
  reporter controls.
- Automated scanner output with no demonstrated impact.

## Supported versions

Diffuse is pre-1.0 and ships from `main`. Fixes land on `main`; there are no
maintained release branches. Track `main` and redeploy to receive security
fixes.
