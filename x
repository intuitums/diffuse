#!/bin/sh
# One repository entry point. Keep CI, contributor docs, and local checks on
# the same commands so no environment has a private definition of "green".
set -eu
cd "$(dirname "$0")"

usage() {
  echo "usage: ./x [check|test|integration|lint|fmt] [args...]" >&2
  exit 2
}

command=${1:-check}
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  check)
    [ "$#" -eq 0 ] || usage
    ruff check .
    python -m pytest -m "not integration"
    ;;
  test)
    python -m pytest "$@"
    ;;
  integration)
    # --build is not optional: without it compose reuses a cached image and a
    # green run can validate stale code. The tmpfs database is disposable, so
    # the project is torn down with volumes either way.
    [ "$#" -eq 0 ] || usage
    status=0
    docker compose -f compose.tests.yaml run --build --rm tests || status=$?
    docker compose -f compose.tests.yaml down -v
    exit "$status"
    ;;
  lint)
    ruff check . "$@"
    ;;
  fmt)
    ruff format "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    ;;
esac
