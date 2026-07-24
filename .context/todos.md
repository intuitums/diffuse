# Diffuse follow-up todos

Captured from the post-Claude review pass on `review-diffuse-claude-draft`
(2026-07-24). Higher-severity bugs from that pass are already fixed in
`efbbe54`. These remaining items are medium severity and intentionally parked
rather than expanded into the foundation PR.

## Open

### 1. Do not activate `new` lineages without a recorded finding thread

- **Why:** `mark_publication_published` can activate `new` finding lineages
  even when inline attach failed and `record_finding_threads` no-oped on an
  empty comment set. Active findings without `finding_threads` cannot be
  resolved/reopened, targeted by `@diffuse` conversation, or scheduled for
  feedback sync.
- **Where:** `service/github_review.py`, `service/gitlab_review.py`,
  `service/finding_store.py` (`record_finding_threads`, publication mark path).
- **Acceptance:**
  - A publish path that fails or skips inline attach for a `new` finding must
    not leave that lineage `active` without a root comment/thread row.
  - Prefer one of: keep the transition pending, retry inline attach, or create
    an explicit non-inline thread anchor before activation.
  - Add a regression test covering partial/failed inline attach with summary
    fallback publication.

### 2. Annotate GitHub checks from still-open lineages, not only current-report findings

- **Why:** Check conclusion uses `continuity.open_findings` /
  `unresolved_findings`, but annotations iterate `report.findings` only. A
  check can fail for an older still-open finding that was not re-emitted this
  run, with no annotation pointing at it.
- **Where:** `service/github_check.py`.
- **Acceptance:**
  - Annotations for failing conclusions include RIGHT-side locations from
    unresolved open lineages, not only findings present in the current report.
  - Avoid duplicate annotations when the same lineage is also in
    `report.findings`.
  - Add a unit test where an older open finding flips the conclusion and
    appears as an annotation.

### 3. Bound GitHub webhook delivery replay / unsigned delivery-ID growth

- **Why:** GitHub HMAC covers the body only. `X-GitHub-Delivery` is
  client-controlled and unsigned, and there is no timestamp/max-age (unlike
  GitLab Standard Webhooks). A captured legitimate webhook can be replayed with
  new delivery IDs; business dedup usually prevents re-review, but each unique
  delivery still inserts into `scm_webhook_deliveries` and does lock/DB work.
- **Where:** `service/github.py` (`verify_signature`),
  `service/webhook_server.py`, `service/workflow.py` (delivery insert before
  revision dedup).
- **Acceptance:**
  - Treat `(payload_sha256, event_name)` (or equivalent) as a first-class
    dedup/rate-limit key before or at delivery insert, and/or TTL-bound unique
    delivery retention.
  - Document that GitHub’s HMAC cannot bind delivery IDs.
  - Add a test that repeated bodies with distinct delivery IDs do not unbounded-
    grow durable delivery work beyond the chosen policy.

## Done nearby

- Incomplete GitLab update-diff fail-closed (no full-MR continuity fallback)
- Rename-aware finding continuity / path aliases
- Bounded webhook body reads before buffering
- Idempotency lease fencing (`lease_generation`, migration `0004`)
- Zero-count hunk sides no longer invent retrieval ranges
- Feedback-sync API base URL normalization
- Due-work scheduler starvation backoff
- `persist_review_report` regenerate clears unpublished lineage events first
- Service tokens reject reuse of `DIFFUSE_API_TOKEN`
