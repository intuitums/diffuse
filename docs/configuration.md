# Diffuse v1 review configuration

Diffuse v1 has one repository-owned configuration file:
`.diffuse/config.json` at the repository root. It controls whether Diffuse
reviews the repository, the paths it ignores, the severity floor, draft pull
request behavior, and the bounded review plan.

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

- Diffuse never submits a GitHub `APPROVE` review. Any non-empty
  `auto_approval` setting is rejected.
- Nested `.diffuse/config.json` files, custom context, cross-repository
  context, automatic learned rules, and autonomous fixes are not supported v1
  configuration. Transitional code may still parse some of those fields while
  the removal migration lands; they are not a supported contract and must not
  be used for a new setup.
- Human-authored guidance and feedback remain part of the roadmap, but feedback
  cannot silently change or suppress correctness/security findings.

Use [v1-scope.md](v1-scope.md) for the product boundary. The former exhaustive
policy reference was removed because it described deferred behavior as a
shipping feature.
