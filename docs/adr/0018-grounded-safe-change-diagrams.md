# ADR 0018: Grounded safe change diagrams

Date: 2026-07-23

Status: Accepted

## Context

Greptile's review anatomy documents automatically selected sequence,
entity-relation, class, and flow diagrams, while omitting diagrams for minimal
changes. Its version-controlled output settings allow diagram inclusion,
collapse, and default-open behavior to be configured.

Mermaid is executable rendering syntax, not inert prose. Passing arbitrary
model output through a fenced block would permit links, callbacks, directives,
or embedded content and would make trivial reviews consume unnecessary model
budget.

Public references:

- <https://www.greptile.com/docs/code-review/first-pr-review>
- <https://www.greptile.com/docs/code-review/greptile-config-reference>

## Decision

- Run a dedicated structured diagram stage only for at least 40 changed lines,
  or at least 12 changed lines spanning two or more reviewable files.
- Give the stage only bounded reviewable diff chunks and bounded retrieved
  context. Permit it to return no diagram when the relationships are not
  grounded or prose is clearer.
- Support sequence, entity-relation, class, and flow diagrams. Require the
  declared kind to match the first Mermaid directive.
- Reject Markdown fences, init directives, clicks/callbacks, URLs, HTML
  elements, styling directives, control characters, more than 200 lines, long
  lines, or more than 12,000 source characters.
- Persist only validated kind, title, and Mermaid source on the immutable
  review report. Preserve collapse/default-open presentation with that report.
- Default diagram output on, but let cascading path policy configure inclusion,
  collapse, and default-open. Any touched reviewable path that disables
  inclusion vetoes the PR-level diagram.

## Consequences

Non-trivial reviews can communicate architecture and flow without allowing
untrusted repository instructions or unconstrained model syntax to become
active review content. Small reviews incur no diagram inference call.

This foundation does not yet support manual requests for a chosen diagram type,
diagram-specific regeneration, visual syntax compilation before publication,
or output controls for every non-diagram section.
