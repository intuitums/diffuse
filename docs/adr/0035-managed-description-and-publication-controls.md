# ADR 0035: Managed descriptions and immutable publication controls

Date: 2026-07-24

Status: Accepted

## Context

The public `greptile.json` contract can place review output in a pull-request
description, disable the main status comment, and disable AI-fix prompts.
Diffuse already separated review generation from retryable provider
publication, so resolving any of these settings at publication time would let
repository changes alter a previously generated review.

Updating a PR/MR description also emits a provider `edited` webhook. Treating
that event as a new automatic trigger would create a same-head review loop.
Replacing the whole description would risk losing human-authored content, and
blind writes could apply an old report to a newer head.

Public provider contracts:

- <https://www.greptile.com/docs/code-review/greptile-json-reference>
- <https://docs.github.com/en/rest/pulls/pulls>
- <https://docs.gitlab.com/api/merge_requests/>

## Decision

- Resolve `update_description`, `summary_comment`, and `fix_with_agent` through
  cascading repository policy and copy the PR/MR-level values onto the durable
  review report.
- When description targeting is enabled, render the complete report inside one
  reserved Diffuse marker pair. Preserve every byte outside that region and
  replace the region idempotently on retry. Description output takes
  precedence over a top-level summary comment.
- Fetch the current provider object immediately before mutation and require the
  expected repository identity, number, open state, web URL, and reviewed head.
  GitHub also sends the returned ETag as `If-Match` when available. Validate the
  mutation response against the same identity and exact persisted body.
- Continue publishing eligible exact-line findings when the top-level summary
  is disabled. Avoid empty provider comments when neither a summary nor inline
  findings is required; the durable publication receives a stable synthetic
  identity.
- When fix guidance is disabled, omit fix-one/fix-all prompts and suggested-fix
  blocks from provider output. Keep verified finding evidence, the durable
  suggested fix, and authorized MCP access unchanged.
- On an `edited` webhook, strip at most one well-formed managed region from the
  previous and current descriptions. Ignore the event only if at least one
  valid Diffuse review region exists and the remaining human-authored text is
  unchanged. Malformed markers or any other relevant metadata change follow
  the ordinary trigger path.

## Consequences

Repositories can migrate all three public publication preferences without a
lossy compatibility exception. GitHub and GitLab retries converge on one
commit-linked description region, preserve human text, and refuse stale-head
writes. The loop guard is content-based rather than actor-based, so it works
with tokens, apps, and self-managed instances without trusting a mutable bot
login.

Disabling both the summary and eligible inline comments intentionally produces
no visible review comment; findings and status decisions remain durable. A
malformed or duplicated reserved region blocks description mutation instead of
guessing which human content is safe to replace.
