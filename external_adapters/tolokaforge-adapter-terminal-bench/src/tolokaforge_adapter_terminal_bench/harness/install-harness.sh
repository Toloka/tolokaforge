#!/bin/sh
# Install one coding-harness CLI into a terminal-bench task image.
#
# Runs inside the harness image layer the adapter synthesises on top of a
# task's base image. Takes the npm package and the exact version to install,
# both resolved from the adapter's HARNESSES table. An unrecognised or
# unpinned invocation aborts the build rather than producing an image whose
# missing or drifting CLI would surface as a trial-time failure.
#
# POSIX sh on purpose: the base image is the task's, and a task is free to
# ship one without bash.
set -eu

PACKAGE="${1:-}"
VERSION="${2:-}"

fail() {
    echo "install-harness.sh: $1" >&2
    exit 1
}

[ -n "$PACKAGE" ] || fail "no npm package given (argument 1)"
[ -n "$VERSION" ] || fail "no version given for '${PACKAGE}' (argument 2); the agent version is part of a benchmark result and must be pinned"

if ! command -v npm >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        apt-get update
        apt-get install -y --no-install-recommends nodejs npm
        rm -rf /var/lib/apt/lists/*
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache nodejs npm
    else
        fail "npm is absent and neither apt-get nor apk is available to install it"
    fi
fi

npm install -g "${PACKAGE}@${VERSION}"
