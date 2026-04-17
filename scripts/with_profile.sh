#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/common.sh"

# STANDARD PREAMBLE: END (do not edit)

# Profile-only wrapper: loads ~/.profile (via common.sh) for PATH setup,
# but does NOT load .env. Use this for tools that handle .env internally
# (e.g., the dev-mcp server uses python-dotenv).
#
# Compare with with_env.sh which also loads .env variables.
#
# Usage: scripts/with_profile.sh <command> [args...]
# Example: scripts/with_profile.sh uv run --package dev-mcp dev-mcp

main() {
    if [[ $# -eq 0 ]]; then
        log_error "Usage: $0 <command> [args...]"
        log_info "Example: $0 uv run --package dev-mcp dev-mcp"
        exit 1
    fi

    log_debug "Executing command: $*"
    exec "$@"
}

main "$@"
