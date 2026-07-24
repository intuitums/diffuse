# ADR 0011: Grounded review-thread conversations

Date: 2026-07-23

Status: Accepted

## Context

Greptile's public product contract lets developers reply to a review comment
with an explicit mention to ask for clarification, alternatives, tests, or
related codebase patterns. It also documents a silent path for ordinary human
discussion. GitHub delivers new inline discussion comments through the
`pull_request_review_comment` webhook and exposes a dedicated REST endpoint for
replying to the top-level review comment.

A self-hosted implementation cannot treat every comment as a model request.
Doing so would create cost abuse, bot loops, accidental replies to human-only
conversation, repository-data disclosure, unordered turns, and duplicate
answers after worker crashes. It also cannot publish raw model prose or
unverified code citations.

Public references:

- <https://www.greptile.com/docs/code-review/key-features>
- <https://www.greptile.com/docs/code-review/developer-essentials>
- <https://www.greptile.com/docs/code-review/tips-recipes>
- <https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request_review_comment>
- <https://docs.github.com/en/rest/pulls/comments#create-a-reply-for-a-review-comment>

## Decision

- Accept only signed GitHub `pull_request_review_comment` events with action
  `created`.
- Require a human `OWNER`, `MEMBER`, or `COLLABORATOR`, an explicit
  `@diffuse` mention, an open pull request, and a reply whose top-level root is
  a stored Diffuse finding thread for the same repository and pull request.
- Ignore bots, unassociated actors, acknowledgements, comments without the
  mention, unrelated roots, and comments beginning with
  `[Human discussion only]`.
- Add path-scoped `review.respond_to_comments`, defaulting to `true`, to the
  immutable cascading repository policy.
- Persist each accepted question and its exact SCM identity before
  acknowledging it. Queue one `answer_review_comment` job pinned to the head
  carried by the signed event.
- Serialize jobs by finding-thread root. A later question cannot bypass an
  older queued retry, preserving causal conversation history.
- Build retrieval from the question, durable finding, and exact commented
  path/line. Use one compatible immutable snapshot and pass the original diff
  hunk plus a bounded history of published turns to the model.
- Treat every human, diff, repository, and prior-message value as untrusted
  prompt data. Parse a strict structured response and retain a code reference
  only when its complete range exists in the finding or retrieved context.
- Persist answer text, validated references, model, prompt version, snapshot,
  usage, and publication attempts before posting.
- Reply through GitHub's top-level review-comment reply endpoint. Neutralize
  generated mentions and include a hidden marker keyed by the source comment
  ID. A retry lists existing review comments and recovers a matching reply on
  the same root rather than posting again.

## Consequences

Developers can have ordered, contextual conversations on Diffuse findings
without turning normal review discussion into inference traffic. Database and
GitHub create-window retries preserve one durable answer per explicit question.
Operators can disable replies for an entire repository or a directory subtree,
and stored provenance is suitable for later cost, quality, and feedback
analysis.

ADR 0031 subsequently extends this workflow to Diffuse-owned GitLab diff
discussions with inherited Developer-or-higher membership checks and
same-discussion publication. The foundation still does not answer top-level or
arbitrary-line questions or provide organization/team authorization and
dashboard controls. Those remain part of the parity roadmap.
