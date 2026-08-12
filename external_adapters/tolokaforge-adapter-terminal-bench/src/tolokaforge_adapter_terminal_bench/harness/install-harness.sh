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

# Modern coding-harness CLIs (claude-code, codex, gemini-cli) use optional
# chaining and other ES2020+ syntax in their install scripts. Ubuntu 22.04's
# apt `nodejs` package ships Node 12, which parses those as SyntaxError. Pull
# Node 20 LTS from NodeSource so the build succeeds regardless of what the
# task's base image ships.
NODE_MAJOR=20

need_node() {
    command -v node >/dev/null 2>&1 || return 0
    node -e "process.exit(parseInt(process.versions.node.split('.')[0]) >= 18 ? 0 : 1)" 2>/dev/null || return 0
    return 1
}

if need_node; then
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        apt-get update
        apt-get install -y --no-install-recommends ca-certificates curl gnupg
        mkdir -p /etc/apt/keyrings
        curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
            | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
        echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
            > /etc/apt/sources.list.d/nodesource.list
        apt-get update
        apt-get install -y --no-install-recommends nodejs
        rm -rf /var/lib/apt/lists/*
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "nodejs~${NODE_MAJOR}" npm
    else
        fail "Node ${NODE_MAJOR}+ is required and neither apt-get nor apk is available to install it"
    fi
fi

npm install -g "${PACKAGE}@${VERSION}"
