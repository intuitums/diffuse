#!/bin/sh

case "$1" in
    *Username*)
        printf '%s\n' "${DIFFUSE_GIT_USERNAME:-git}"
        ;;
    *Password*)
        printf '%s\n' "${DIFFUSE_GIT_TOKEN:-}"
        ;;
    *)
        exit 1
        ;;
esac
