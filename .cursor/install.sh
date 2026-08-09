#!/usr/bin/env bash
# Cursor Cloud Agent install script (runs during Builds / after checkout).
# Idempotent: safe to re-run on a partially prepared disk.
# Do not start long-lived services here — that belongs in start.sh.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

ensure_apt_packages() {
  local -a missing=()
  local pkg
  for pkg in "$@"; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
      missing+=("${pkg}")
    fi
  done
  if ((${#missing[@]} == 0)); then
    return 0
  fi
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

ensure_pgdg_repo() {
  # Some Cursor bases ship a stale pgdg.list without a usable signing key.
  # Disable it before any apt-get update, refresh the keyring, then re-enable.
  if [[ -f /etc/apt/sources.list.d/pgdg.list ]]; then
    sudo mv /etc/apt/sources.list.d/pgdg.list /etc/apt/sources.list.d/pgdg.list.disabled
  fi

  ensure_apt_packages ca-certificates curl gnupg
  sudo install -d -m 0755 /usr/share/postgresql-common/pgdg

  local tmp_asc tmp_gpg
  tmp_asc="$(mktemp)"
  tmp_gpg="$(mktemp)"
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "${tmp_asc}"
  gpg --batch --yes --dearmor -o "${tmp_gpg}" "${tmp_asc}"
  sudo install -m 0644 "${tmp_gpg}" /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  rm -f "${tmp_asc}" "${tmp_gpg}"

  . /etc/os-release
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  sudo rm -f /etc/apt/sources.list.d/pgdg.list.disabled
  sudo apt-get update -y
}

# If Postgres is missing, or a pgdg source is already present, refresh the key
# before any other apt work so a stale source cannot break apt-get update.
if ! command -v pg_lsclusters >/dev/null 2>&1 || [[ -f /etc/apt/sources.list.d/pgdg.list ]]; then
  ensure_pgdg_repo
fi

ensure_apt_packages python3.12 python3.12-venv build-essential

if ! command -v pg_lsclusters >/dev/null 2>&1; then
  # postgresql-17 creates cluster 17/main; pgvector is required by the frozen v1 baseline.
  ensure_apt_packages postgresql-17 postgresql-client-17 postgresql-17-pgvector
fi

if ! command -v pg_lsclusters >/dev/null 2>&1; then
  echo "PostgreSQL 17 client/tools are still missing after install." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3.12 -m venv .venv
fi

# Order matches DEVELOPMENT.md: lock first, then dev tooling, then editable package.
.venv/bin/pip install --upgrade pip
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install --no-deps -e .
.venv/bin/pip check
.venv/bin/diffuse --help >/dev/null

echo "Cursor install complete: .venv ready"
