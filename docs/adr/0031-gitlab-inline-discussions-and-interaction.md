# ADR 0031: GitLab inline discussions and review interaction

## Status

Accepted

## Context

ADR 0030 made GitLab webhook ingestion, diff access, summary publication, and
commit statuses native, but findings were not attached to diff lines. That
prevented the durable finding lineage from owning a GitLab discussion and left
resolve/reopen, clarification, and learning behavior GitHub-only.

GitLab diff discussions require the exact `base_commit_sha`,
`head_commit_sha`, and `start_commit_sha` from one merge-request version. A
discussion response contains both a stable discussion ID and numeric note IDs.
GitLab Note Hooks identify the new note but do not carry enough trusted thread
position or repository-authority information, so safe interaction requires API
enrichment.

Relevant public contracts:

- <https://docs.gitlab.com/api/discussions/>
- <https://docs.gitlab.com/api/project_members/>
- <https://docs.gitlab.com/api/emoji_reactions/>
- <https://docs.gitlab.com/user/project/integrations/webhook_events/>

## Decision

### Exact-line publication

- Version the provider-neutral pull-request event again and retain GitLab's
  authoritative `start_sha` alongside base/head. Older payloads default start
  to base for compatibility, while newly enriched GitLab events require the
  real version value.
- Parse the exact reviewed unified diff and map every eligible new finding to
  its old/new path and LEFT/RIGHT changed line.
- Create one GitLab discussion per eligible finding with all three version
  SHAs. Added lines send only `new_line`; removed lines send only `old_line`.
- Put the stable finding fingerprint marker in the root note. Before creating
  anything, list existing discussions and recover matching roots so a remote
  success followed by a local failure cannot duplicate a discussion.
- Store the numeric root-note ID and stable discussion ID on the durable finding
  thread. A 400/422 position rejection is bounded to that finding and forces
  the complete report into the summary; authorization, transport, and server
  failures still fail the publication attempt.

### Thread state

- Represent address/reopen as the same durable lineage operations used for
  GitHub.
- Reply once with an operation marker, then use GitLab's discussion mutation to
  resolve or reopen the stored discussion ID. Read the discussion first and
  recover an existing reply/state on retry.
- Never locate a thread by path, line, title, or fuzzy body matching after its
  publication.

### Conversation and feedback

- Accept authenticated GitLab Note Hooks for newly created merge-request
  comments. Ignore Diffuse-authored markers before API enrichment.
- Retrieve the current MR, the exact discussion containing the delivered note,
  and the actor's inherited project membership. Only Developer-or-higher users
  can create feedback or `@diffuse` conversation work.
- Require the discussion root to carry a Diffuse finding marker and derive the
  trusted file, side, line, and comment revision from its API position.
- Store ordinary authorized replies as inspectable context feedback. An
  explicit `@diffuse` question additionally enters the existing ordered,
  snapshot-pinned conversation workflow and publishes its answer into the same
  GitLab discussion with a per-question recovery marker.
- Preserve `[Human discussion only]` as an exclusion from both inference and
  feedback.
- Reconcile GitLab award emoji with the same low-priority durable jobs used for
  GitHub reactions. Retain only thumbs-up/down from Developer-or-higher project
  members and append withdrawals rather than rewriting history.

## Consequences

GitHub and GitLab now share one durable model for exact finding locations,
lineage, addressed/reopened thread state, grounded clarification, and
inspectable feedback. Provider-specific API identities remain explicit, so a
GitLab discussion is not impersonated as a GitHub review comment.

GitLab Note Hook acceptance now depends on bounded API reads for the MR,
discussion, and project membership. A discussion that is not visible yet
returns a retryable response instead of being treated as an unrelated comment.
The process-level GitLab token must have sufficient API/project access to read
membership and emoji and to create/reply to/resolve discussions.

Top-level and arbitrary human thread conversations, encrypted per-installation
credentials, and provider-specific quality evaluations remain parity work. ADR
0032 subsequently adds GitLab top-level manual review commands and exact-head
automatic approval.
