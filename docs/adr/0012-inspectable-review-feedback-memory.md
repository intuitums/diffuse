# ADR 0012: Inspectable review-feedback memory

Date: 2026-07-23

Status: Accepted

## Context

Greptile documents continuous learning from team review comments, replies to
its findings, 👍/👎 reactions, and whether suggestions are addressed. It treats
other emoji as neutral and states that security and logic issues remain
protected from adaptive noise filtering.

GitHub supports listing reactions on pull-request review comments, but its
documented webhook catalog has no dedicated review-reaction event. Treating a
`pull_request_review_comment` webhook as a reaction notification would silently
lose feedback. Accepting reactions from arbitrary public-repository users would
also make preference memory vulnerable to poisoning.

Public references:

- <https://www.greptile.com/docs/how-greptile-works/memory-and-learning>
- <https://www.greptile.com/docs/code-review/training-the-learning-system>
- <https://www.greptile.com/docs/code-review/developer-essentials>
- <https://docs.github.com/en/rest/reactions/reactions#list-reactions-for-a-pull-request-review-comment>
- <https://docs.github.com/en/rest/collaborators/collaborators#check-if-a-user-is-a-repository-collaborator>
- <https://docs.github.com/en/webhooks/webhook-events-and-payloads>

## Decision

- Store a human reply as context only when the signed GitHub event identifies
  an owner, member, or collaborator and the reply root maps to a Diffuse finding
  in the exact repository, pull request, and file.
- Treat `[Human discussion only]` as an explicit exclusion from both responses
  and memory capture.
- Create durable sync state with each published finding thread. The worker
  periodically schedules low-priority, leased, retryable
  `sync_review_feedback` jobs rather than relying on a nonexistent reaction
  webhook.
- List reactions through the configured GitHub/GHES REST endpoint. Ignore bots
  and non-thumb emoji, and call GitHub's collaborator endpoint before accepting
  a human actor's 👍 or 👎.
- Append immutable `observed` and `withdrawn` reaction events instead of
  overwriting history. Retries use provider IDs and stable event keys.
- Project addressed and reopened lineage transitions into commit-outcome
  signals in the review-publication transaction.
- Snapshot category and severity on every signal. Mark critical, security, and
  correctness signals as protected from future suppression.
- Expose per-finding feedback summaries from durable state. ADR 0013 defines
  the later human-approved suggested-rule path; raw signals still never alter
  review output directly.

## Consequences

Self-hosted operators retain an auditable account of which authorized feedback
was seen, removed, or contradicted by later code. Public users cannot train a
repository merely by reacting to a comment, and neutral emoji do not acquire
accidental polarity. A failed API call retries without interpreting a partial
response as mass reaction removal.

ADR 0031 subsequently extends reply and reaction collection to authorized
GitLab finding discussions. The scheduler currently continues polling while
the repository remains enabled. Closed-PR retirement, top-level human review
comments, merged-but-unaddressed outcomes, preference ranking,
organization/team moderation, and a management UI/API remain required for full
team-memory parity.
