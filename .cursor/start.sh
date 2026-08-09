#!/usr/bin/env bash
# Cursor Cloud Agent start script (runs on every agent boot after install).
# Idempotent: start Postgres, ensure local roles/dirs, leave app/worker alone.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

DIFFUSE_GIT_HOME="${DIFFUSE_GIT_HOME:-/home/ubuntu/.diffuse-git-home}"
REPOSITORY_ROOT="${DIFFUSE_REPOSITORY_ROOT:-/var/lib/diffuse/repositories}"
PG_PASSWORD="${POSTGRES_PASSWORD:-diffuse-dev}"

if ! command -v pg_lsclusters >/dev/null 2>&1; then
  echo "PostgreSQL 17 is not installed; cannot start the cloud stack." >&2
  exit 1
fi

cluster_status="$(
  pg_lsclusters --no-header 2>/dev/null \
    | awk '$1 == "17" && $2 == "main" { print $4 }'
)"
if [[ "${cluster_status}" != "online" ]]; then
  sudo pg_ctlcluster 17 main start
fi

for _ in $(seq 1 30); do
  if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  echo "PostgreSQL 17 did not become ready on 127.0.0.1:5432" >&2
  exit 1
fi

# Role + databases match AGENTS.md (SUPERUSER required for frozen v1 vector extension).
# Password is the documented cloud-dev value unless POSTGRES_PASSWORD is set in the environment.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'diffuse') THEN
    CREATE ROLE diffuse LOGIN SUPERUSER PASSWORD '${PG_PASSWORD}';
  ELSE
    ALTER ROLE diffuse WITH LOGIN SUPERUSER PASSWORD '${PG_PASSWORD}';
  END IF;
END
\$\$;
SQL

for db in diffuse diffuse_test; do
  exists="$(sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='${db}'")"
  if [[ "${exists}" != "1" ]]; then
    sudo -u postgres createdb -O diffuse "${db}"
  fi
done

# Rewrite-free git home so RepositoryMirror remote checks match production.
mkdir -p "${DIFFUSE_GIT_HOME}"
if [[ ! -f "${DIFFUSE_GIT_HOME}/.gitconfig" ]]; then
  : >"${DIFFUSE_GIT_HOME}/.gitconfig"
fi

sudo mkdir -p "${REPOSITORY_ROOT}"
sudo chown -R "$(id -u):$(id -g)" "$(dirname "${REPOSITORY_ROOT}")"

# Local .env for cloud agents. Never overwrite an existing file (may hold secrets).
if [[ ! -f .env ]]; then
  python3.12 - <<'PY'
from pathlib import Path

example = Path(".env.example").read_text()
lines = []
for line in example.splitlines():
    if line.startswith("POSTGRES_PASSWORD="):
        lines.append("POSTGRES_PASSWORD=diffuse-dev")
    elif line.startswith("DATABASE_URL="):
        lines.append(
            "DATABASE_URL=postgresql://diffuse:diffuse-dev@127.0.0.1:5432/diffuse"
        )
    elif line.startswith("DIFFUSE_ALLOW_PLAINTEXT_ORIGINS="):
        lines.append("DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1")
    elif line.startswith("DIFFUSE_REPOSITORY_ROOT="):
        lines.append("DIFFUSE_REPOSITORY_ROOT=/var/lib/diffuse/repositories")
    else:
        lines.append(line)
Path(".env").write_text("\n".join(lines) + "\n")
PY
fi

echo "Cursor start complete: PostgreSQL online, mirror root ready"
