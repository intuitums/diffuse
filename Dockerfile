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

COPY evals ./evals
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
        --add-data /src/service/git_askpass.sh:service \
        --add-data /src/sql:sql \
        --add-data /src/evals:evals \
        /src/service/runtime.py \
    && test -x /build/diffuse/diffuse \
    && test -f /build/diffuse/_internal/sql/schema.sql \
    && test -f /build/diffuse/_internal/evals/baseline.example.json \
    && test -x /build/diffuse/_internal/service/git_askpass.sh \
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

COPY requirements.txt requirements-dev.txt pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r requirements-dev.txt \
    && python -m pip check

COPY indexer ./indexer
COPY repository_policy ./repository_policy
COPY retriever ./retriever
COPY service ./service
COPY sql ./sql
COPY tests ./tests

ENTRYPOINT ["python", "-m", "pytest"]

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
    DIFFUSE_GIT_ASKPASS=/opt/diffuse/_internal/service/git_askpass.sh \
    DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/diffuse:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' \
        > /etc/apt/apt.conf.d/keep-cache \
    && apt-get update \
    && apt-get install -y --no-install-recommends adduser ca-certificates git \
    && addgroup --system diffuse \
    && adduser --system --ingroup diffuse --home /home/diffuse diffuse \
    && mkdir -p /opt/diffuse /var/lib/diffuse/repositories \
    && chown -R diffuse:diffuse /opt/diffuse /var/lib/diffuse

COPY --from=builder --chown=diffuse:diffuse /build/diffuse/ /opt/diffuse/
COPY --chown=diffuse:diffuse LICENSE /opt/diffuse/LICENSE

RUN chmod 500 /opt/diffuse/diffuse \
    && chmod 500 /opt/diffuse/_internal/service/git_askpass.sh \
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
