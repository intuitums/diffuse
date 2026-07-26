# syntax=docker/dockerfile:1.11

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends binutils

COPY requirements.txt requirements.lock requirements-build.txt requirements-build.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r requirements.lock \
    && python -m pip install --require-hashes -r requirements-build.lock \
    && python -m pip check

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
        --add-data /src/service/git_askpass.sh:service \
        --add-data /src/sql:sql \
        /src/service/runtime.py \
    && test -x /build/diffuse/diffuse \
    && test -f /build/diffuse/_internal/sql/schema.sql \
    && test -x /build/diffuse/_internal/service/git_askpass.sh \
    && ! find /build/diffuse -type f \
        \( -name '*.py' -o -name '*.pyc' -o -name '*.pyo' \) -print -quit \
        | grep -q .

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS test-runner

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
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
    apt-get update \
    && apt-get install -y --no-install-recommends adduser ca-certificates git \
    && addgroup --system diffuse \
    && adduser --system --ingroup diffuse --home /home/diffuse diffuse \
    && mkdir -p /opt/diffuse /var/lib/diffuse/repositories \
    && chown -R diffuse:diffuse /opt/diffuse /var/lib/diffuse

COPY --from=builder --chown=diffuse:diffuse /build/diffuse/ /opt/diffuse/

RUN chmod 500 /opt/diffuse/diffuse \
    && chmod 500 /opt/diffuse/_internal/service/git_askpass.sh \
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
