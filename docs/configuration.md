# Diffuse v1 review configuration

Diffuse v1 has one supported repository-owned configuration file:
`.diffuse/config.json` at the repository root. It controls whether Diffuse
reviews the repository, the paths it ignores, the severity floor, draft pull
request behavior, and the bounded review plan.

Separately from configuration, policy discovery indexes guidance documents
for the review agents: `.diffuse/rules.md`, plus convention-named instruction
files (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and
`.github/copilot-instructions.md`).
`repository_policy/discovery.py` is authoritative for the exact set.

```json
{
  "version": 1,
  "review": {
    "enabled": true,
    "passes": ["correctness", "security", "tests"],
    "minimum_severity": "medium",
    "ignored_paths": ["generated/**", "**/*.snap"]
  },
  "triggers": {
    "review_drafts": false,
    "status_check": true
  }
}
```

`passes` is the current review-plan input. Its final `standard` and `deep`
mapping is delivered with the CLI review-team contract; do not interpret it as
permission to run an unbounded agent swarm. `status_check` controls the
GitHub Check only. It never authorizes a pull request.

## Deliberate v1 limits

- Diffuse never submits a GitHub `APPROVE` review. Any `auto_approval` setting
  is rejected with a clear error.
- `review.fix_with_agent` has been removed; Diffuse v1 does not publish agent
  handoffs. Suggested-fix text may still appear on findings when present.
- Nested `.diffuse/config.json` layers and `.diffuse/files.json` custom
  context are implemented and active in policy discovery today, but they are
  not part of the supported v1 configuration contract — do not build a new
  setup on them until v1 scope admits them. The same caveat applies to other
  fields the schema currently accepts beyond the example above (such as
  `context.repos`, `security.preventative`, and the output-shaping fields);
  `repository_policy/models.py` is authoritative. Automatic learned rules and
  autonomous fixes are not supported v1 configuration.
- Human-authored guidance and feedback remain part of the roadmap, but feedback
  cannot silently change or suppress correctness/security findings.

Use [v1-scope.md](v1-scope.md) for the product boundary. The former exhaustive
policy reference was removed because it described deferred behavior as a
shipping feature.
