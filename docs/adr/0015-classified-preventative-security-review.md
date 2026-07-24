# ADR 0015: Classified preventative security review

Date: 2026-07-23

Status: Accepted

## Context

Greptile's public April 13, 2026 changelog describes improved vulnerability
detection, security badges, and an optional preventative review mode for risky
patterns that are not yet exploitable. A self-hosted implementation must not
present a hypothetical future exploit as a current vulnerability, let an
ordinary style concern acquire a security badge, or allow preventative noise
to bypass repository confidence policy.

Public references:

- <https://www.greptile.com/changelog>
- <https://www.greptile.com/docs/code-review-bot/getting-started>

## Decision

- Classify every security candidate and finding as either `vulnerability` or
  `preventative`. Reject classifications on non-security findings.
- Treat an omitted classification on security findings as `vulnerability` for
  backward-compatible structured output and existing durable callers.
- Define a vulnerability as currently exploitable through a concrete
  attacker-controlled source, reachable path, and sink or security-invariant
  break. Define preventative as not currently exploitable but unsafe after a
  concrete future trust-boundary or caller change.
- Keep preventative review disabled by default. Enable it with cascading
  `security.preventative` policy and apply the stricter of the ordinary
  confidence floor and `security.preventative_minimum_confidence`.
- Reject preventative findings with critical or high severity. Do not relabel
  or silently cap model output whose claimed impact contradicts its subtype.
- Give both candidate generation and independent verification the same
  classification contract, then apply deterministic policy filters after
  model output.
- Include classification in candidate deduplication, finding fingerprints, and
  lineage compatibility. Persist it on findings, feedback signals, and
  suggested-rule evidence.
- Label SCM review comments, summary rows, and check annotations distinctly as
  a security vulnerability or preventative security risk.

## Consequences

Operators can opt into forward-looking security advice without weakening the
meaning of a vulnerability badge or increasing default review noise. Durable
memory retains what the finding originally claimed, and addressed/reopened
tracking cannot merge unlike security classes.

This is a security-review foundation, not a quality claim. Versioned
vulnerability/preventative eval corpora, measured recall and false-positive
gates, dependency-aware analysis, and dashboard controls remain parity work.
