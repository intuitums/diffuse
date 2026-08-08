# Security Policy

## Reporting a vulnerability

**Do not report security vulnerabilities in a pull request, an issue, or any
other channel that a third party can read.**

Email **security@intuitum.xyz**. Include:

- the affected release version or image digest;
- the deployment shape (bundled Compose profile, external PostgreSQL, other);
- the provider involved (GitHub Cloud, GitHub Enterprise, or none);
- reproduction steps or a proof of concept; and
- the impact you believe it has.

Report privately rather than opening a public issue, so a fix can ship before
the details are public.

## What to expect

- Acknowledgement of your report within 5 business days.
- An initial assessment, including whether we consider it in scope, within
  10 business days.
- Progress updates until the issue is resolved or closed.
- Attribution in the public release note, unless you ask us not to.

Diffuse has no paid bug bounty. Coordinated disclosure is expected: please give
us a reasonable opportunity to ship a fix and notify affected operators before
disclosing publicly.

## Scope

Diffuse is self-hosted: operators run it inside their own environment, against
their own repositories, with their own credentials. Reports are most useful when
they describe something an operator cannot simply configure away.

### Diffuse processes untrusted repository content

This is the defining property of the threat model. Diffuse clones repositories,
indexes their source, reads in-repository configuration (`.diffuse` and
agent/editor instruction files), ingests webhook payloads, and
feeds diffs, retrieved code, and pull-request text to a
language model. **All of that content is attacker-influencable** — by an
external contributor opening a pull request, by a compromised dependency
vendored into a repository, or by anyone who can get a branch pushed.

The following are explicitly **in scope**:

- **Prompt injection.** Repository content, diffs, pull-request titles and
  descriptions, commit messages, review threads, or in-repository configuration
  that steers the review model or an agent-CLI session into leaking data it was
  given, taking an unintended action (publishing, approving, re-triggering),
  suppressing findings for other changes, or escaping the constraints of the
  review runtime.
- **Sandbox and isolation escape.** Anything in repository content, a crafted
  filename, a symlink, a Git attribute, submodule, hook, or LFS pointer that
  causes code execution, filesystem access, or network access outside the
  disposable worktree or mirror — including escaping the repository root,
  bypassing `DIFFUSE_MAX_REPOSITORY_BYTES`, or reaching the host from a
  container. For agent-CLI local review (when selectable): escaping the
  Diffuse-owned child environment, weakening or bypassing the CLI OS sandbox
  policy Diffuse writes, loading a repository- or user-supplied MCP server
  despite `--strict-mcp-config`, or running below the version floor so
  sandbox settings are silently ignored.
- **Credential exposure.** Any path that leaks the GitHub App private key,
  an installation token, the OAuth client secret, `DIFFUSE_API_TOKEN`, or a
  repository-scoped service token into a clone URL sent to an unintended
  origin, into model input, into published review output, into logs, or into an
  image layer.
- **Authorization bypass.** Reading or writing another repository's index,
  findings, analytics, or custom context across a repository-scoped token
  boundary; escalating a read scope to a generation or write scope; acting on a
  repository the token is not assigned to.
- **Webhook authentication flaws.** The one webhook route, `POST
  /webhook/github`, authenticates a delivery solely by an HMAC-SHA-256
  `X-Hub-Signature-256` header, and deduplicates by a durable unique
  `(provider, base URL, delivery id)` record. Signature-verification bypass,
  replaying a delivery past that record, or forging an instance origin are all
  in scope.
- **OAuth and session flaws** in the browser sign-in path (`/auth/cli`,
  `/auth/github/callback`, `/setup`), including state fixation or reuse,
  session-token exposure, and open redirects. These routes are mounted and
  reachable even though no request authenticator consumes the session they
  mint yet; they still spend the client secret and write identity rows.
- **MCP and REST API flaws**, including DNS-rebinding protection bypass and
  authentication bypass.
- **Database migration integrity** failures that allow unverified SQL to be
  applied.

### Agent-CLI review boundary

Diffuse's target architecture is control plane + isolated agent-runner: the
worker never executes a CLI or mounts agent credentials. Session capabilities
and structured results are defined in `service.agents.contract`. The
self-hosted controlled pilot can select `REVIEW_RUNTIME=claude` or `codex` and
dispatch it to the matching runner; local review stays on the transitional API
path until it uses that same session contract. The boundaries are:

1. **Child environment allowlist** — credentials Diffuse does not name never
   reach the CLI process (`GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `AWS_*`,
   and a scratch `PATH` so `gh` is not resolvable by name).
2. **CLI OS sandbox** — empty network allowlist, `failIfUnavailable`,
   credential file/env denies, worktree-scoped reads; written as persistent
   settings under the Diffuse-owned config directory.
3. **Version floor** — refuse builds that would silently drop those settings;
   refuse native Windows where the CLI has no OS sandbox.

MCP servers configured into that session run **outside** the CLI sandbox with
full host privileges. Diffuse must construct `--mcp-config` itself, always pass
`--strict-mcp-config`, and keep its own MCP server to index queries only — never
executing repository-supplied content. Reports that break any of those
invariants are in scope.

#### Self-hosted container decision

The self-hosted agent-runner drives an agent CLI only from a dedicated review
compartment. The invariant is not `sandbox.enabled == true`; it is: **the
process that reads untrusted content holds no control-plane credential and
reaches no resource whose compromise matters.** The worker and API container do
not meet that invariant, so neither is an acceptable CLI host.

This is measured rather than assumed. On 2026-08-05, on `intuitumserver-1`
(Ubuntu 26.04), a `debian:trixie-slim` probe image with `bubblewrap` and
`uidmap` ran `bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-uts
--ro-bind / / --proc /proc /bin/true` as uid 10001. `newuidmap` was setuid and
`/etc/subuid` had ranges. Network unsharing was deliberately omitted after the
initial `--unshare-all` probe failed while configuring loopback: the agent needs
network access.

| Container configuration | Result |
| --- | --- |
| `cap_drop: ALL` + `no-new-privileges` | FAIL — `No permissions to create new namespace` |
| plus `apparmor=unconfined` | FAIL — same (seccomp blocks first) |
| plus `seccomp=unconfined` | FAIL — `Failed to make / slave: Permission denied` (AppArmor denies mount) |
| both unconfined | FAIL — `setting up uid map: Permission denied` |
| both unconfined, without no-new-privileges | FAIL — same |
| both unconfined plus `CAP_SYS_ADMIN` | FAIL — same |
| `--privileged` | FAIL — same |

The host reported `kernel.apparmor_restrict_unprivileged_userns = 1`, AppArmor
enabled, and Docker security options `apparmor`, `seccomp/builtin`, and
`cgroupns`. Loosening any of these controls did not produce a usable Bubblewrap
boundary; disabling the host-wide user-namespace restriction would weaken every
workload on the host. Diffuse will not ask operators to do that.

Accordingly, `agent_host.sandbox_settings` has a distinct
`CONTAINER_COMPARTMENT_PROFILE` that renders `sandbox.enabled: false` only as a
declaration that the CLI sandbox is unavailable. Selecting it is not enough to
use it: `sandbox_settings` refuses to render that profile unless it is given a
`CompartmentAssertion` produced by `assert_compartment` from a preflight that
passed, and an assertion minted for one profile is not accepted for another. An
adapter that selects the profile and skips the preflight therefore gets an
error rather than an unsandboxed review.

The preflight is implemented in `service.review.agent_compartment`. The runner
executes it at startup and obtains a fresh profile-matching assertion before
each CLI session, checking identity, read-only root and agent home, absence of
control-plane credentials, database isolation, and constrained model egress.
The existing local CLI policy separately remains fail-closed with
`failIfUnavailable: true`.

### Out of scope

- Vulnerabilities in the language model provider itself, or model output that is
  merely low quality, wrong, or unhelpful without a security consequence.
- Vulnerabilities solely in a third-party agent CLI binary with no Diffuse
  misconfiguration or boundary bypass (report those upstream).
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

Diffuse is pre-1.0. Fixes land on `main` and ship as signed, digest-pinned
release artifacts; there are no maintained release branches, so security fixes
are not backported. Upgrade to the latest release to receive them. See
[`deploy/README.md`](deploy/README.md) for signature verification.

Because operators control their own hosts, a fix is only effective once the
operator upgrades.
