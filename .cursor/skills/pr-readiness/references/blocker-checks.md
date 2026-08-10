# Blocker and warning scans

Targeted scans for Phase 3. These are language-agnostic classes of defect, each with patterns for
the ecosystems commonly encountered. Apply only the patterns matching languages actually in the
diff, and treat the lists as starting points rather than a closed set — a repository in a language
not listed here gets the same scan classes, expressed in that language's idioms.

**Scope.** These scans catch release blockers a code reviewer would not raise: a committed `.env`, a
build artifact, a conflict marker, a missing migration. They are a pre-filter, not a review. Logic
defects go to `/review-bugbot` and exploitability to `/review-security`, per Composition in
`SKILL.md` — a pattern hit here is never the security verdict on its own.

Scope every scan to the files changed in the audited range:

```
git diff --name-only --diff-filter=d <merge-base>..HEAD    # changed, still present
git ls-files --others --exclude-standard                   # untracked
```

Apply patterns with `git grep -nE '<pattern>' -- $(git diff --name-only --diff-filter=d <merge-base>..HEAD)`,
or `grep -nE` over an explicit file list when untracked files are in scope. Prefer scanning added
lines only — `git diff <merge-base>..HEAD | grep -nE '^\+.*<pattern>'` — so pre-existing issues are
not attributed to this change. Batch long file lists rather than writing a temp file.

A hit is a candidate, not a finding — open the line and judge it in context before reporting.

## Unresolved merge conflicts

Blocker whenever present in a source file.

```
git grep -nE '^(<{7}|={7}|>{7})( |$)' -- <changed files>
git ls-files -u                      # unmerged index entries
git status --porcelain | grep -E '^(UU|AA|DD|AU|UA|DU|UD)'
```

Also check for an interrupted operation: `.git/MERGE_HEAD`, `.git/REBASE_HEAD`,
`.git/CHERRY_PICK_HEAD`, or the equivalent state in another VCS.

## Accidentally committed artifacts

Blocker when a build output, dependency tree, local environment file, or editor state entered the
diff. Lockfile changes are legitimate when dependencies changed and suspicious otherwise.

Flag added paths matching build and dependency output — `node_modules/`, `dist/`, `build/`, `out/`,
`.next/`, `.nuxt/`, `target/`, `bin/`, `obj/`, `_build/`, `.gradle/`, `DerivedData/`, `Pods/`,
`vendor/` (unless the repository vendors deliberately), `__pycache__/`, `*.pyc`, `.venv/`,
`.terraform/`, `.dvc/cache/` — plus coverage and log output (`coverage/`, `.nyc_output/`, `*.log`),
OS and editor state (`.DS_Store`, `Thumbs.db`, `.idea/`, `.vscode/` unless already tracked), merge
and backup debris (`*.orig`, `*.rej`, `*.bak`, `*.swp`), and credential-shaped files (`.env`,
`.env.*` except examples, `*.pem`, `*.key`, `*.p12`, `*.keystore`, `id_rsa`, `.netrc`,
`terraform.tfstate`, `*.tfvars` with real values, `kubeconfig`, service-account JSON).

```
git diff --name-only --diff-filter=A <merge-base>..HEAD
git check-ignore -v -- <suspect-path>          # would the ignore file have caught it?
git diff --stat <merge-base>..HEAD | sort -k3 -n | tail   # unusually large files
```

Treat any newly added binary over ~1 MB as a blocker unless the repository clearly stores binaries
or uses LFS. Flag LFS pointer files whose content cannot be verified.

## Debug and temporary code

Blocker when left in shipped paths; warning in test or script paths where it may be intentional.

```
console\.(log|debug|dir|trace)|debugger;          # JS, TS
\bprint\(|pdb\.set_trace|breakpoint\(\)           # Python
fmt\.Print(ln|f)?\(|spew\.Dump|log\.Print         # Go
dbg!\(|eprintln!\(|todo!\(                        # Rust
binding\.pry|byebug                               # Ruby
System\.out\.print|printStackTrace|println!       # Java, Kotlin
var_dump\(|dd\(|dump\(|error_log\(                # PHP
Console\.WriteLine|Debug\.WriteLine               # .NET
IO\.inspect|IO\.puts                              # Elixir
print\(|debugPrint\(|NSLog\(                      # Swift, Dart
echo +\$|set -x                                   # shell
SELECT \* FROM|-- test                            # ad hoc SQL left in place
```

Also flag: commented-out blocks of formerly live code; sleeps added outside tests; hardcoded
`localhost`, `127.0.0.1`, personal hostnames, ports, or absolute local paths in non-config code; and
newly added suppressions (`eslint-disable`, `# noqa`, `# type: ignore`, `@ts-ignore`,
`@ts-expect-error`, `#[allow(`, `nolint`, `NOSONAR`, `checkov:skip`, `tflint-ignore`) that mask a
problem this change introduced.

Disabled or focused tests are a blocker when they silence coverage for the changed behavior:

```
\.only\(|\.skip\(|fdescribe|fit\(|xit\(|xdescribe
@pytest\.mark\.(skip|xfail)|t\.Skip\(|#\[ignore\]|@Ignore|@Disabled
```

## Placeholders and unfinished work

Warning normally; blocker when on a code path the change depends on.

```
\b(TODO|FIXME|XXX|HACK|WIP|TBD)\b
REPLACE_ME|CHANGEME|YOUR_[A-Z_]+_HERE|<placeholder>
lorem ipsum|foo(bar)?@example\.com|asdf|test123
not implemented|NotImplementedError|unimplemented!\(|panic!\("todo|TODO\(
```

Keep the word boundaries — an unanchored `XXX` matches `mktemp` templates such as `config.XXXXXX`
and floods the report with false positives.

Restrict to lines the diff added:

```
git diff <merge-base>..HEAD | grep -nE '^\+.*\b(TODO|FIXME|XXX|HACK)\b'
```

## Suspicious secrets

Blocker on any credible hit. Do not print the secret value in the report — cite `path:line` and the
kind of credential only. Never edit, rotate, or delete the secret without approval; recommend
rotation when a real credential reached a commit.

High-signal patterns on added lines:

```
AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}       # AWS
gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}
glpat-[A-Za-z0-9_\-]{20,}               # GitLab
sk-ant-[A-Za-z0-9_\-]{20,}              # Anthropic
sk-(proj-)?[A-Za-z0-9]{20,}             # OpenAI-style
xox[baprs]-[A-Za-z0-9-]{10,}            # Slack
AIza[0-9A-Za-z_\-]{35}                  # Google
sk_live_|pk_live_|rk_live_              # Stripe and similar live keys
-----BEGIN [A-Z ]*PRIVATE KEY-----
eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.        # JWT
(api[_-]?key|secret|passwd|password|token|bearer)\s*[:=]\s*['"][^'"]{8,}['"]
(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp)://[^:@\s]+:[^@\s]+@
```

Discount obvious test fixtures, example files, and documented dummy values — but say the hit was
reviewed and dismissed rather than omitting it.

If a secret scanner is available (`gitleaks`, `trufflehog`, `detect-secrets`, or one configured in
CI), prefer it as authoritative and cite its output:

```
gitleaks detect --no-banner --redact --log-opts '<merge-base>..HEAD'
```

When no scanner is installed, the patterns above are the fallback — do not install one.

## Unrelated changes

Warning. Compare each changed file against the inferred intent. Typical signals: formatting-only
churn across untouched files, dependency bumps unrelated to the feature, config or CI edits with no
connection to the task, and drive-by refactors. Report them as candidates to split into a separate
change rather than as defects.

```
git diff --stat <merge-base>..HEAD           # files far from the task's center of gravity
git log --oneline <merge-base>..HEAD         # commits whose subject does not match the intent
```

## Missing migrations

Blocker when a schema or model change ships without its migration.

| Schema change in | Expected migration |
| --- | --- |
| `prisma/schema.prisma` | new directory under `prisma/migrations/` |
| Django `models.py` | new file under `<app>/migrations/` |
| Rails models, `db/schema.rb` | new file under `db/migrate/` |
| SQLAlchemy models | new revision under `alembic/versions/` |
| Drizzle schema | new SQL under `drizzle/` plus updated journal |
| TypeORM, Sequelize, Knex entities | new file under the configured migrations directory |
| Laravel models | new file under `database/migrations/` |
| Ecto schemas | new file under `priv/repo/migrations/` |
| EF Core models | new class under `Migrations/` |
| Go structs with `goose`, `golang-migrate`, `atlas` | new numbered pair in the migrations directory |
| Flyway, Liquibase | new versioned SQL or changelog entry |
| `sqitch.plan` | new change with deploy, revert, and verify scripts |
| Supabase, Hasura | new file under `supabase/migrations/` or metadata migration |
| `*.sql` schema files | matching versioned migration file |
| Convex `convex/schema.ts` | no migration files; verify the change is backward compatible for existing documents, and check for a backfill when a field becomes required |

Also flag the reverse: a migration with no corresponding model change, a migration edited after it
was presumably applied, two migrations claiming the same version, and a destructive migration
(dropping a column or table) with no stated rollout plan.

## Generated output and contract drift

Blocker when committed generated output no longer matches its source, because CI regenerates and
compares.

Check for a source change without its regenerated companion: protobuf or gRPC stubs, GraphQL or
OpenAPI clients, ORM client output, mocks, i18n catalogs, generated docs or CLI reference, snapshot
tests, `go.sum`/lockfile entries for changed manifests, and `CHANGELOG` fragments in
changeset-driven repositories.

Verify without running a writing regenerator: compare timestamps and content, look for a CI step
that regenerates and diffs, and use read-only comparison tools when present
(`buf breaking --against <base ref>`, `graphql-inspector diff`, API compatibility checkers).

Also flag manifest and lockfile desync — a dependency added to the manifest but absent from the
lockfile, or vice versa.

## Infrastructure and configuration risk

Blocker when an infrastructure change would destroy or expose resources. Judge by reading, never by
applying:

- Resource removals or renames in Terraform, Pulumi, CloudFormation, or Bicep that imply
  destroy-and-recreate on stateful resources.
- Loosened security posture: public buckets, `0.0.0.0/0` ingress, disabled TLS, disabled auth,
  wildcard IAM actions, permissive CORS, debug mode enabled in production config.
- Lowered resource limits, removed health checks, or changed replica counts in Kubernetes manifests.
- Feature flags defaulted on, or environment-specific values hardcoded into shared config.

## Documentation and changelog gaps

Warning. Check that the change updated what it invalidated:

- Public API, CLI flags, or environment variables changed without documentation updates.
- New environment variable absent from the example env file or deployment docs.
- A repository that maintains a changelog with no entry for a user-visible change.
- Stale doc comments or type documentation on functions whose signature changed.
- A PR template or `CONTRIBUTING.md` requirement the branch does not satisfy.
- Missing license headers where the repository applies them consistently.

## Commit and branch hygiene

Warning unless repository instructions make it mandatory.

```
git log --format='%h %s' <merge-base>..HEAD
git log --format='%an %ae' <merge-base>..HEAD | sort -u
```

Flag: `wip`, `fixup!`, `squash!`, `temp`, empty or one-word subjects, merge commits when the
repository rebases, commits authored by an unexpected identity, oversized commits that should be
split, and branch names violating a documented convention.
