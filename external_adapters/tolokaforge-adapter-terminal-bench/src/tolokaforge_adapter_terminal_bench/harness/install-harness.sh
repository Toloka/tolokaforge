#!/bin/sh
# Install one coding-harness CLI into a terminal-bench task image.
#
# Runs inside the harness image layer the adapter synthesises on top of a
# task's base image. Takes exactly one argument: the harness name. An
# unrecognised name aborts the build rather than producing an image whose
# missing CLI would surface as a trial-time "command not found".
#
# POSIX sh on purpose: the base image is the task's, and a task is free to
# ship one without bash.
set -eu

ACCEPTED="terminus-2 claude-code codex gemini-cli"
HARNESS="${1:-}"

fail() {
    echo "install-harness.sh: $1" >&2
    exit 1
}

ensure_node() {
    if command -v npm >/dev/null 2>&1; then
        return
    fi
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        apt-get update
        apt-get install -y --no-install-recommends nodejs npm
        rm -rf /var/lib/apt/lists/*
        return
    fi
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache nodejs npm
        return
    fi
    fail "npm is absent and neither apt-get nor apk is available to install it"
}

case "$HARNESS" in
    terminus-2)
        ;;
    claude-code)
        ensure_node
        npm install -g @anthropic-ai/claude-code
        ;;
    codex)
        ensure_node
        npm install -g @openai/codex
        ;;
    gemini-cli)
        ensure_node
        npm install -g @google/gemini-cli
        ;;
    *)
        fail "unknown harness '${HARNESS}'; accepted: ${ACCEPTED}"
        ;;
esac
