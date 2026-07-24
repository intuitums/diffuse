# ADR 0010: Durable finding lineage and review-thread state

- Status: accepted
- Date: 2026-07-23

## Context

A finding is not an isolated comment. Teams need to know whether the same issue
remains open after code moves, whether a later commit addressed it, and whether
it returned. Posting every review as unrelated inline comments creates noise
and loses the history needed for status checks, analytics, agents, and future
team learning.

Greptile's public behavior marks corresponding comments addressed after a
commit touches their flagged files, and its developer surfaces distinguish
addressed from unaddressed comments. GitHub exposes REST review-comment replies
and GraphQL mutations that resolve or unresolve a review thread. Remote comment
state alone is not sufficient because worker retries and partial writes must be
recoverable.

## Decision

- Every verified finding belongs to a pull-request-scoped
  `finding_lineages` record. Review occurrences and transitions are immutable
  `finding_lineage_events`.
- The worker fetches GitHub's exact compare diff from the last published review
  head to the current normalized head. Both old and new paths count as touched,
  preserving deletion and rename evidence.
- Matching first uses the exact finding fingerprint. A bounded deterministic
  similarity score then tolerates line movement and minor title/body changes,
  but never matches across paths or categories.
- A current match to an active lineage is `persistent`; a match to an addressed
  lineage is `reopened`; an unmatched current finding is `new`. An unmatched
  active lineage becomes `addressed` only when its file appears in the exact
  update diff. Same-commit reruns cannot address findings.
- Transitions remain provisional until SCM review publication succeeds. The
  publication transaction activates them. Superseded or terminally failed
  unpublished reviews delete their provisional events and pending lineages.
- Only `new` findings create GitHub inline comments. Their root comment IDs are
  persisted. Persistent findings remain on their existing thread.
- Addressed and reopened events create durable, independently retryable thread
  operations. Diffuse posts a reply containing a stable hidden event marker,
  then resolves or unresolves the corresponding GitHub review thread through
  GraphQL. A retry searches for the marker before writing.
- Review summaries report new, still-open, reopened, and addressed counts.
  Status checks evaluate all active lineages, preventing an older blocking
  finding from disappearing solely because a later model invocation omitted
  it.
- Repository-authored finding text remains bounded and mention-neutralized at
  every GitHub publication surface.

## Consequences

One logical defect has one durable history and, where inline publication is
available, one GitHub root thread. Review updates become quieter, addressed
state is queryable without scraping GitHub, and crash recovery does not
duplicate replies. The state model is ready for future addressed-rate
analytics, MCP retrieval, agent fixes, and human feedback.

The foundation intentionally uses conservative deterministic matching rather
than another model call. ADR 0012 adds authorized reply, reaction, and
commit-outcome capture. Diffuse does not yet infer lineage across file renames,
apply learned preference ranking, expose manual resolution controls, or
evaluate provider-specific thread-state behavior at scale.
