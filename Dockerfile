# syntax=docker/dockerfile:1.11

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder

# PIP_NO_CACHE_DIR is deliberately NOT set here. Every pip invocation below runs
# under `--mount=type=cache,target=/root/.cache/pip`, and the two directly
# contradict each other: with the cache disabled the mount never accumulates
# anything, so every build re-downloaded and re-built every wheel while looking
# like it had a cache. The mount is a BuildKit cache, not a layer, so nothing it
# holds reaches the image.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# The base image ships /etc/apt/apt.conf.d/docker-clean, which deletes downloaded
# .debs immediately after install. That defeats the apt cache mounts below exactly
# the way PIP_NO_CACHE_DIR defeated the pip one, so keep the packages instead.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' \
        > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends binutils

# Only the .lock files, on purpose. This is the most expensive layer in the build,
# and copying requirements.txt / requirements-build.txt in alongside them meant a
# comment edit in either range file invalidated a full dependency install that
# never reads them -- the installs below are `--require-hashes` against the locks.
COPY requirements.lock requirements-build.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r requirements.lock \
    && python -m pip install --require-hashes -r requirements-build.lock \
    && python -m pip check

# Only the two files pyproject.toml's data-files list ships, not the whole
# directory. evals/fixtures/ holds .py sources used as review fixtures, and
# `--add-data /src/evals:evals` below is recursive -- copying those in would put
# .py files in the frozen image and trip the assertion at the end of the
# pyinstaller step. They are development tooling for scripts/eval.sh and have no
# place in the runtime image.
COPY evals/baseline.example.json evals/README.md ./evals/
COPY indexer ./indexer
COPY repository_policy ./repository_policy
COPY retriever ./retriever
COPY service ./service
COPY sql ./sql

RUN pyinstaller \
        --noconfirm \
        --clean \
        --onedir \
        --name diffuse \
        --distpath /build \
        --workpath /tmp/pyinstaller \
        --specpath /tmp/pyinstaller-spec \
        --collect-data litellm \
        --collect-data mcp \
        --collect-submodules mcp.server \
        --collect-submodules mcp.shared \
        --collect-submodules uvicorn.lifespan \
        --collect-submodules uvicorn.loops \
        --collect-submodules uvicorn.protocols \
        --collect-submodules indexer \
        --collect-submodules repository_policy \
        --collect-submodules retriever \
        --collect-submodules service \
        --collect-data tiktoken \
        --collect-submodules tiktoken_ext \
        --copy-metadata tree-sitter \
        --copy-metadata tree-sitter-c \
        --copy-metadata tree-sitter-cpp \
        --copy-metadata tree-sitter-go \
        --copy-metadata tree-sitter-java \
        --copy-metadata tree-sitter-javascript \
        --copy-metadata tree-sitter-php \
        --copy-metadata tree-sitter-ruby \
        --copy-metadata tree-sitter-rust \
        --copy-metadata tree-sitter-typescript \
        --add-data /src/service/hosted/git_askpass.sh:service/hosted \
        --add-data /src/sql:sql \
        --add-data /src/evals:evals \
        /src/service/runtime.py \
    && test -x /build/diffuse/diffuse \
    && test -f /build/diffuse/_internal/sql/schema.sql \
    && test -f /build/diffuse/_internal/evals/baseline.example.json \
    && test -x /build/diffuse/_internal/service/hosted/git_askpass.sh \
    && for dist in tree_sitter tree_sitter_c tree_sitter_cpp tree_sitter_go \
            tree_sitter_java tree_sitter_javascript tree_sitter_php \
            tree_sitter_ruby tree_sitter_rust tree_sitter_typescript; do \
        ls -d /build/diffuse/_internal/"${dist}"-*.dist-info >/dev/null 2>&1 \
            || { echo "missing parser metadata: ${dist}" >&2; exit 1; }; \
    done \
    && ! find /build/diffuse -type f \
        \( -name '*.py' -o -name '*.pyc' -o -name '*.pyo' \) -print -quit \
        | grep -q .

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS test-runner

# See the builder stage: PIP_NO_CACHE_DIR would make the pip cache mount below inert.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# See the builder stage for why docker-clean goes away before the cache mounts.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' \
        > /etc/apt/apt.conf.d/keep-cache

# `git`, because the suite shells out to it. Twenty tests across test_index_repo,
# test_repositories, test_repository_policy, and test_review_cli build throwaway
# repositories with `git init` and drive the real binary; without it they fail
# with `FileNotFoundError: 'git'`, which reads like a code bug and is not one.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends git

# The lock FIRST, hash-checked, exactly as the builder stage and CI do it. This
# stage used to install requirements-dev.txt alone, which re-resolves the
# version RANGES in requirements.txt on every build: it produced litellm 1.95.0,
# fastapi 0.141.1, and openai 2.53.0 against a shipped image pinned to 1.93.0,
# 0.139.2, and 2.48.0. The stage meant to test what ships was the one stage not
# testing it.
#
# requirements-dev.txt goes on top and adds test tooling only. It re-states
# requirements.txt, but every range there is already satisfied by the locked
# version installed above, so pip leaves those alone rather than upgrading them.
# `pip check` only confirms the resulting graph is consistent — it would still
# pass if a range pulled a locked package forward. The freeze comparison in
# verify.yml's `test-container` job is what catches that.
COPY requirements.lock requirements.txt requirements-dev.txt pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r requirements.lock \
    && python -m pip install -r requirements-dev.txt \
    && python -m pip check

# Source under test.
COPY indexer ./indexer
COPY repository_policy ./repository_policy
COPY retriever ./retriever
COPY service ./service
COPY sql ./sql
COPY tests ./tests

# Files the suite reads as fixtures rather than imports. Each one is load-bearing
# for a test that fails without it, and each was absent while nothing built this
# stage: evals/ for the harness fixtures (test_eval_harness), .env.example and
# deploy/env.example for the documented-configuration checks
# (test_env_documentation, test_deploy_env_example), both Compose profiles for
# the compartment-boundary assertions, and the workflow definitions for the
# release-provenance assertions.
COPY evals ./evals
COPY deploy ./deploy
COPY .env.example ./.env.example
COPY docker-compose.yml ./docker-compose.yml
COPY .github ./.github
# CI helper imported by tests/test_check_lock_freeze.py (and invoked from the
# host in verify.yml). Without it the container suite fails at collection with
# `No module named 'scripts'`, which reads like a packaging bug and is not one.
COPY scripts ./scripts
# This file, read as text: test_release_artifacts_ship_the_license asserts that
# the runtime stage below installs the BSL text, because a release that ships
# without it is a licensing problem rather than a functional one.
COPY Dockerfile ./Dockerfile

# Migrate first when an integration database is configured, then hand every
# argument to pytest. The integration suite connects to an already-migrated
# database and fails with `relation "repositories" does not exist` against an
# empty one -- CI's test-postgres job runs `diffuse database migrate` as a
# separate step, and a container that did not would make the container path
# look broken when only its setup was missing.
#
# Unset POSTGRES_TEST_DATABASE_URL means a unit-only run, which needs no
# database and must not wait for one.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'if [ -n "${POSTGRES_TEST_DATABASE_URL}" ]; then' \
    '    DATABASE_URL="${POSTGRES_TEST_DATABASE_URL}" \' \
    '        python -m service.cli.review database migrate >&2' \
    'fi' \
    'exec python -m pytest "$@"' \
    > /usr/local/bin/run-tests \
    && chmod +x /usr/local/bin/run-tests

ENTRYPOINT ["/usr/local/bin/run-tests"]

FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS runtime

ARG DIFFUSE_VERSION=0.1.0
ARG DIFFUSE_REVISION=unknown
ARG DIFFUSE_CREATED=unknown

LABEL org.opencontainers.image.title="Diffuse" \
      org.opencontainers.image.description="Proprietary self-hostable code intelligence and review platform" \
      org.opencontainers.image.version="${DIFFUSE_VERSION}" \
      org.opencontainers.image.revision="${DIFFUSE_REVISION}" \
      org.opencontainers.image.created="${DIFFUSE_CREATED}" \
      org.opencontainers.image.source="https://github.com/intuitumxyz/Diffuse"

ENV DIFFUSE_SQL_DIR=/opt/diffuse/_internal/sql \
    DIFFUSE_GIT_ASKPASS=/opt/diffuse/_internal/service/hosted/git_askpass.sh \
    DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/diffuse:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1

# The uid/gid is part of the volume contract: an operator must be able to inspect
# or repair the durable agent credential directory from the host. `adduser
# --system` otherwise allocates the next free ID, which makes that operation
# non-deterministic across releases. Docker copies the image-path metadata onto a
# new empty named volume; a worker with cap_drop=ALL cannot repair the root-owned
# 0755 default after the fact, so assert the mode in the build.
#
# `agent/home` is created here and not only by `agent_login_home()`. Compose
# sets HOME to it for the opt-in `agent-runner` service (not the worker), but
# that helper only runs during `agent login` / `agent logout`, so on a stack
# that has never signed in to an agent the runner would boot pointing at a
# directory that does not exist. Both are on the volume path, so Docker seeds
# them onto a new named volume together.

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' \
        > /etc/apt/apt.conf.d/keep-cache \
    && apt-get update \
    && apt-get install -y --no-install-recommends adduser ca-certificates git \
    && addgroup --system --gid 10001 diffuse \
    && adduser --system --uid 10001 --ingroup diffuse --home /home/diffuse diffuse \
    && mkdir -p /opt/diffuse /var/lib/diffuse/repositories \
    && install -d --owner=diffuse --group=diffuse --mode=700 /var/lib/diffuse/agent \
    && install -d --owner=diffuse --group=diffuse --mode=700 /var/lib/diffuse/agent/home \
    && chown -R diffuse:diffuse /opt/diffuse /var/lib/diffuse/repositories \
    && test "$(stat -c '%u:%g:%a' /var/lib/diffuse/agent)" = '10001:10001:700' \
    && test "$(stat -c '%u:%g:%a' /var/lib/diffuse/agent/home)" = '10001:10001:700'

COPY --from=builder --chown=diffuse:diffuse /build/diffuse/ /opt/diffuse/
COPY --chown=diffuse:diffuse LICENSE /opt/diffuse/LICENSE

RUN chmod 500 /opt/diffuse/diffuse \
    && chmod 500 /opt/diffuse/_internal/service/hosted/git_askpass.sh \
    && test -f /opt/diffuse/LICENSE \
    && ! find /opt/diffuse -type f \
        \( -name '*.py' -o -name '*.pyc' -o -name '*.pyo' \) -print -quit \
        | grep -q .

USER diffuse

EXPOSE 8000

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=30s \
    CMD ["diffuse", "healthcheck"]

ENTRYPOINT ["diffuse"]
CMD ["serve"]
