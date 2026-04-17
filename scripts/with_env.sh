#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/common.sh"

# STANDARD PREAMBLE: END (do not edit)

# Main logic
main() {
    # Check if we have any arguments to execute
    if [[ $# -eq 0 ]]; then
        log_error "Usage: $0 <command> [args...]"
        log_info "Example: $0 uv run example-app hello"
        exit 1
    fi

    log_debug "Setting up environment..."

    # Setup environment (handles validation internally)
    if ! "${SCRIPT_DIR}/setup/setup_env.sh"; then
        log_error "Failed to setup environment"
        exit 1
    fi

    # Load the environment using common function
    load_env_file "${REPO_DIR}/.env"

    # Execute the provided command
    log_debug "Executing command: $*"
    exec "$@"
}

# Check if REPO_DIR is available
if [[ -z "${REPO_DIR+x}" ]]; then
    log_error "REPO_DIR is not set. Cannot determine repository directory."
    exit 1
fi

# Run main function with all arguments
main "$@"
