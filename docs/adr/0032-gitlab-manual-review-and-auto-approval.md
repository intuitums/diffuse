# ADR 0032: GitLab manual review and exact-head automatic approval

## Status

Accepted

## Context

ADR 0031 gave GitLab merge requests exact-line discussions and authorized
interaction inside Diffuse finding threads. Two visible review controls remained
provider-specific: an authorized top-level `@diffuse` comment could not request
another GitLab review, and the conservative automatic-approval decision could
only publish on GitHub.

A GitLab Note Hook identifies a note and actor but does not prove project
authority or whether the note is a top-level MR comment. GitLab's approval API
accepts an optional head SHA and rejects a mismatch with `409`, but GitLab also
warns automation to wait until approval reset processing and diff patch-ID
calculation finish. Approval responses have no per-approval ID or reviewed
commit, and the aggregate `approved` boolean has different Community and
Enterprise Edition semantics.

Relevant public contracts:

- <https://docs.gitlab.com/user/project/integrations/webhook_events/>
- <https://docs.gitlab.com/api/project_members/>
- <https://docs.gitlab.com/api/merge_requests/>
- <https://docs.gitlab.com/api/merge_request_approvals/>

## Decision

### Manual review commands

- Use the same bounded, line-anchored `@diffuse` command parser for GitHub and
  GitLab.
- For a newly created GitLab MR note, reject Diffuse-generated markers before
  API access, then require inherited Developer-or-higher membership.
- Retrieve the current MR and exact discussion containing the delivered note.
  Only the root note of a top-level discussion can become a manual review;
  replies on Diffuse findings retain their conversation/feedback behavior.
- Re-enrich authoritative current base/head/start, metadata, lifecycle, fork
  source-project identity, and changed-file count. Store the event as
  `action=manual`, `trigger_kind=manual`, and `trigger_id=note:<id>`.
- The provider/host-scoped webhook delivery preserves replay idempotency, while
  a different note ID deliberately creates another same-head review.

### Automatic approval

- Apply the existing default-off, strictest-wins deterministic eligibility
  decision unchanged to GitLab.
- Immediately before approval, require the MR to remain open, non-draft, and on
  the reviewed head.
- Fail retryably while `detailed_merge_status` is `checking` or
  `approvals_syncing`, or while the matching diff version has no non-null
  `patch_id_sha`. This prevents approval from racing GitLab's approval-reset
  processing.
- Resolve the authenticated token user and call
  `POST /projects/:id/merge_requests/:iid/approve` with the exact reviewed
  `sha`.
- Accept publication only when the returned `approved_by` contains that user.
  Do not use the edition-dependent aggregate `approved` boolean.
- Treat `409` as a stale-head cancellation. If an exact-SHA request reports that
  the token user cannot add a duplicate approval, read `/approvals` and recover
  only when that same authenticated user is already present. The deterministic
  external identity includes provider, repository, MR, user, and reviewed head.

## Consequences

GitHub and GitLab now share manual rerun and conservative auto-approval product
behavior while retaining provider-native authorization and publication safety.
Manual Note Hook acceptance adds bounded membership, MR, and discussion reads.
Automatic approval adds current-MR, diff-version, and authenticated-user reads
before its exact-SHA mutation.

The process token must be an eligible GitLab approver. Projects that require
interactive password or SAML reauthentication cannot be auto-approved by this
token and fail visibly. Encrypted per-installation approval credentials,
organization policy, an operator kill switch, and provider-specific quality
evaluations remain future work.

ADR 0033 subsequently exposes the same authoritative GitLab manual-review
workflow through the repository-authorized MCP trigger.
