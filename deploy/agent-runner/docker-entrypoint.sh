#!/bin/sh
# Entrypoint for the isolated Diffuse agent-runner image.
# Gate A/B skeleton: prove the pinned CLIs exist and refuse control-plane secrets.
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

refuse_control_plane_secrets() {
    # Soft-empty defaults are set in the image ENV. A compose env_file or
    # operator export that injects real values must not reach this process.
    for name in DATABASE_URL GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY GITHUB_WEBHOOK_SECRET; do
        eval "value=\${$name-}"
        if [ -n "$value" ]; then
            fail "agent-runner refuses control-plane secret $name"
        fi
    done
}

self_check() {
    command -v claude >/dev/null 2>&1 || fail "claude is not on PATH"
    command -v codex >/dev/null 2>&1 || fail "codex is not on PATH"
    claude --version >/dev/null
    codex --version >/dev/null
    # uid is part of the credential-volume contract (matches control-plane image).
    uid="$(id -u)"
    if [ "$uid" != "0" ] && [ "$uid" != "10001" ]; then
        fail "agent-runner must run as uid 10001 (or root only during image build)"
    fi
}

status() {
    refuse_control_plane_secrets
    self_check
    printf 'diffuse-agent-runner: ready\n'
    printf 'claude: %s\n' "$(claude --version 2>&1 | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    printf 'codex: %s\n' "$(codex --version 2>&1 | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    printf 'uid: %s\n' "$(id -u)"
    printf 'DIFFUSE_AGENT_HOME: %s\n' "${DIFFUSE_AGENT_HOME:-}"
}

case "${1:-status}" in
    --self-check)
        self_check
        ;;
    status)
        status
        ;;
    *)
        fail "unknown agent-runner command: $1 (supported: status, --self-check)"
        ;;
esac
