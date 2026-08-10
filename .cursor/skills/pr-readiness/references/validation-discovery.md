# Validation discovery, execution, and CI parity

Guidance for Phases 2 and 4. The goal is to run the checks this repository actually uses, decide
honestly whether they cover what CI will run, and never mutate anything while doing it.

Nothing here is a required stack. Every table below is a lookup keyed on files that are present —
match what exists, ignore the rest, and never report a check for an ecosystem the repository does
not use.

## Discovery order

Stop at the first source that answers the question; consult the rest only to fill gaps.

1. **Repository instructions** — `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, README build and test
   sections, `docs/development*`. If they name the commands, those are the commands.
2. **Task runner** — the entry point the repository standardizes on.
3. **Manifests and tool configuration** — script or target definitions per ecosystem.
4. **CI definitions** — ground truth for what must pass, and the fallback when nothing else
   documents a command.
5. **The user** — when a repository is genuinely undiscoverable, ask rather than guess.

## Task runners

Check for these before ecosystem-specific commands; a repository that has one usually routes
everything through it.

| Signal | Discover |
| --- | --- |
| `Makefile`, `GNUmakefile` | `grep -E '^[a-zA-Z0-9_.-]+:' Makefile`, or `make -qp` |
| `justfile`, `.justfile` | `just --list` |
| `Taskfile.yml` | `task --list-all` |
| `package.json` scripts | list the `scripts` keys |
| `mise.toml`, `.mise.toml` | `mise tasks` |
| `moon.yml`, `.moon/` | `moon query tasks` |
| `Earthfile` | `earthly ls` |
| `dagger.json` | `dagger functions` |
| `nx.json`, `turbo.json` | `nx show projects`, `turbo run --dry=json` |
| `BUILD`, `BUILD.bazel`, `WORKSPACE`, `MODULE.bazel` | `bazel query //...` |
| `pants.toml`, `BUCK`, `.buckconfig` | `pants tailor --check`, `buck2 targets //...` |
| `Rakefile`, `build.xml`, `Justfile` variants | list targets |
| `.pre-commit-config.yaml` | `pre-commit run --files <changed files>` |
| `lefthook.yml`, `.husky/`, `.lintstagedrc*` | read the hook definitions |

## Ecosystem detection

| Signal file | Ecosystem | Typical checks |
| --- | --- | --- |
| `package.json` | Node, TypeScript, Deno-compat | test, lint, typecheck, build, format check |
| `deno.json`, `deno.jsonc` | Deno | `deno test`, `deno lint`, `deno check`, `deno fmt --check` |
| `bun.lockb` | Bun | `bun test`, plus the package scripts |
| `pyproject.toml`, `setup.py`, `requirements*.txt`, `tox.ini`, `noxfile.py` | Python | `pytest`, `ruff check`, `mypy`, `pyright`, `black --check` |
| `go.mod` | Go | `go build ./...`, `go vet ./...`, `go test ./...`, `gofmt -l .` |
| `Cargo.toml` | Rust | `cargo check`, `cargo clippy -- -D warnings`, `cargo test`, `cargo fmt --check` |
| `Gemfile`, `*.gemspec` | Ruby | `bundle exec rspec`, `bundle exec rubocop` |
| `pom.xml`, `build.gradle*`, `settings.gradle*` | Java, Kotlin | `mvn -q verify`, `./gradlew check` |
| `*.sln`, `*.csproj`, `*.fsproj` | .NET | `dotnet build`, `dotnet test`, `dotnet format --verify-no-changes` |
| `composer.json` | PHP | `composer test`, `phpstan analyse`, `pint --test` |
| `mix.exs` | Elixir | `mix test`, `mix format --check-formatted`, `mix credo`, `mix dialyzer` |
| `rebar.config` | Erlang | `rebar3 eunit`, `rebar3 dialyzer` |
| `Package.swift`, `*.xcodeproj`, `*.xcworkspace` | Swift, iOS | `swift build`, `swift test`, `xcodebuild test`, `swiftlint` |
| `pubspec.yaml` | Dart, Flutter | `dart analyze`, `flutter test`, `dart format --output=none --set-exit-if-changed` |
| `build.zig` | Zig | `zig build test`, `zig fmt --check` |
| `build.sbt` | Scala | `sbt test`, `sbt scalafmtCheck` |
| `*.cabal`, `stack.yaml` | Haskell | `stack test`, `cabal build`, `hlint` |
| `dune-project` | OCaml | `dune build`, `dune runtest`, `dune build @fmt` |
| `deps.edn`, `project.clj` | Clojure | `clojure -M:test`, `lein test`, `clj-kondo --lint` |
| `Project.toml` | Julia | `julia -e 'using Pkg; Pkg.test()'` |
| `DESCRIPTION`, `renv.lock` | R | `R CMD check`, `testthat::test_dir` |
| `CMakeLists.txt`, `meson.build`, `configure.ac` | C, C++ | configure and build, `ctest`, `clang-format --dry-run --Werror`, `clang-tidy` |
| `*.nimble` | Nim | `nimble test` |
| `*.sh`, `*.bash`, `.shellcheckrc`, `test/*.bats` | Shell | `shellcheck`, `bats -r test`, `shfmt -d` |
| `*.ps1`, `*.psd1` | PowerShell | `Invoke-Pester`, `Invoke-ScriptAnalyzer` |
| `*.lua`, `.luacheckrc` | Lua | `busted`, `luacheck .`, `stylua --check` |
| `*.pl`, `cpanfile` | Perl | `prove -r t/`, `perlcritic` |
| `Cargo.toml` plus `foundry.toml`, `hardhat.config.*` | Solidity | `forge test`, `forge fmt --check`, `npx hardhat test`, `slither .` |
| `*.tf`, `*.tofu` | Terraform, OpenTofu | `terraform fmt -check`, `terraform validate`, `tflint`, `checkov`/`tfsec` |
| `*.bicep`, `*.arm.json` | Azure IaC | `bicep build`, `az deployment what-if` only against a scratch scope |
| `Chart.yaml`, `kustomization.yaml`, `*.k8s.yaml` | Kubernetes | `helm lint`, `helm template`, `kustomize build`, `kubeconform`, `kubectl --dry-run=client` |
| `ansible.cfg`, `playbook*.yml` | Ansible | `ansible-lint`, `ansible-playbook --syntax-check` |
| `Dockerfile`, `compose.yaml` | Containers | `hadolint`, `docker compose config`, build only if cheap |
| `flake.nix`, `default.nix` | Nix | `nix flake check`, `nix build --dry-run` |
| `dbt_project.yml` | dbt | `dbt parse`, `dbt compile`, `dbt build` only against a dev target |
| `buf.yaml`, `*.proto` | Protobuf | `buf lint`, `buf breaking --against <base ref>` |
| `openapi.yaml`, `*.graphql` | API schemas | `spectral lint`, `graphql-inspector diff`, contract or breaking-change checks |
| `*.ipynb` | Notebooks | `nbstripout --verify`, `nbqa`, execution checks only if cheap |
| `dvc.yaml`, `params.yaml` | Data, ML | `dvc status`, `dvc repro --dry` |
| `mkdocs.yml`, `docusaurus.config.*`, `conf.py`, `book.toml` | Docs sites | build the site, link check |
| `.tex`, `latexmkrc` | LaTeX | `latexmk -pdf -halt-on-error` |
| `Cargo.toml`/`package.json` inside `apps/*`, `packages/*`, `services/*` | Monorepo | scope to affected workspaces |

Detect the package manager from the lockfile before running anything: `package-lock.json` → `npm`,
`pnpm-lock.yaml` → `pnpm`, `yarn.lock` → `yarn`, `bun.lockb` → `bun`, `uv.lock` → `uv`,
`poetry.lock` → `poetry`, `Pipfile.lock` → `pipenv`, `Gemfile.lock` → `bundler`, `go.sum` → go
modules, `Cargo.lock` → cargo. Using the wrong one rewrites the lockfile, which violates audit-only.

Respect version pins when present — `.nvmrc`, `.node-version`, `.python-version`, `.tool-versions`,
`rust-toolchain.toml`, `.sdkmanrc`, `mise.toml`. A check run on the wrong runtime version is partial
evidence at best; say so.

## Choosing what to run

Run the smallest set that covers the diff:

- Docs or comments only → formatting and link checks; skip the suite and say why.
- One package in a monorepo → that package's checks, plus anything downstream that imports it.
- CI, build config, or dependency changes → the build, and the checks that config governs.
- Schema or migration changes → the migration check plus any schema-validation command.
- Infrastructure code → format, validate, lint, and policy checks; never an apply.

Prefer scoped invocations when the tool supports them (`pytest tests/foo`, `go test ./pkg/...`,
`cargo test -p crate`, `bazel test //pkg/...`), and say in the report that the run was scoped.

## Safety rules

**Never run** commands that leave the machine or mutate shared state: deploy, publish, or release
targets; `terraform apply`, `tofu apply`, `pulumi up`, `kubectl apply`, `helm upgrade`; migrations
against a non-local database; `dbt run` against production; package publication; `git push`; forge
CLIs that create or update requests; and anything requiring credentials not already present locally.

**Use check modes, never write modes:**

| Instead of | Run |
| --- | --- |
| `prettier --write` | `prettier --check` |
| `eslint --fix` | `eslint` |
| `ruff format` / `black` | `ruff format --check` / `black --check` |
| `cargo fmt` | `cargo fmt --check` |
| `gofmt -w` | `gofmt -l` |
| `dotnet format` | `dotnet format --verify-no-changes` |
| `terraform fmt` | `terraform fmt -check` |
| `mix format` | `mix format --check-formatted` |
| `swift-format -i` | `swift-format lint` |
| `dart format` | `dart format --output=none --set-exit-if-changed` |

If a check inherently writes (codegen, snapshot update, lockfile refresh, `go mod tidy`), do not run
it. Verify the committed output another way when one exists, or record it as skipped.

**Expensive or unavailable checks.** Time-box each command to roughly five minutes and the phase to
roughly fifteen. Skip and record, rather than waiting out or working around: suites needing a
database, Docker, emulator, simulator, or browser that is not already running; end-to-end, load, and
fuzz suites; full release builds on large repositories; checks needing secrets, VPN, or paid APIs;
anything whose first run downloads a large toolchain.

Never install dependencies, start services, seed databases, or provision infrastructure to make a
check runnable. That is an environment change and requires approval.

**When a tool is absent.** Every helper is optional. If `jq` is missing, read the manifest directly.
If a forge CLI is missing, skip request metadata. If a scanner is missing, fall back to the pattern
scans in `blocker-checks.md`. If the project's own toolchain is missing, record the check as skipped
— never substitute a globally installed version of a tool the project pins.

**Recording results.** For each command capture the exact invocation, exit code, pass or fail, and
for failures a short excerpt identifying the failing test or rule. Attribute a failure to this change
only after checking whether it also fails at the base; when in doubt, state the attribution as
unverified.

## CI parity

Read every CI definition that applies to a change against the resolved base. Detect the system from
what is present:

| Signal | System |
| --- | --- |
| `.github/workflows/*.yml` | GitHub Actions — note `on.pull_request`, `paths` filters, matrices, and reusable workflows referenced by `uses:` |
| `.gitlab-ci.yml`, `.gitlab/ci/` | GitLab CI — follow `include:` |
| `.circleci/config.yml` | CircleCI — follow orbs |
| `Jenkinsfile` | Jenkins |
| `.buildkite/` | Buildkite |
| `azure-pipelines.yml`, `.azure/` | Azure Pipelines |
| `bitbucket-pipelines.yml` | Bitbucket |
| `.drone.yml`, `.woodpecker.yml` | Drone, Woodpecker |
| `.teamcity/`, `.harness/`, `cloudbuild.yaml`, `buildspec.yml` | TeamCity, Harness, Cloud Build, CodeBuild |
| `.pre-commit-config.yaml`, `.husky/`, `lefthook.yml` | Hook-time checks that also gate merges |
| `codecov.yml`, `sonar-project.properties`, `renovate.json` | Coverage, quality, and dependency gates |

Extract each executed step, then build an explicit mapping table for the report:

| CI check | Local equivalent run | Coverage |
| --- | --- | --- |
| lint | same command | covered |
| test with coverage gate | scoped test run | partial — coverage threshold not evaluated |
| container build | none | not covered |

Classify honestly:

- **Covered** — the same command, or a strict superset, ran locally and passed.
- **Partial** — narrower scope, different flags, or a different runtime version. Say what differs.
- **Not covered** — did not run locally. If the diff plausibly affects it, that is a warning; if the
  diff clearly cannot affect it, note it as not applicable instead.

A matrix job counts as partial when only one cell ran locally. Required status checks and branch
protection may be visible through the forge API (`gh api repos/{owner}/{repo}/branches/<base>/protection`
or the equivalent); permission errors are common, so record them as unverified rather than assuming.
When the repository has no CI at all, state that CI parity is not applicable and rely on local
evidence.
