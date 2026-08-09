#!/usr/bin/env bash
# Cursor Cloud Agent install script (runs during Builds / after checkout).
# Idempotent: safe to re-run on a partially prepared disk.
# Do not start long-lived services here — that belongs in start.sh.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "python3.12 is required (see .python-version)" >&2
  exit 1
fi

if ! command -v pg_lsclusters >/dev/null 2>&1; then
  echo "PostgreSQL 17 client/tools are required (pg_lsclusters missing)." >&2
  echo "Install postgresql-17 and postgresql-17-pgvector on this VM base image." >&2
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
