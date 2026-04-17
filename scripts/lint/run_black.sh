#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"

# STANDARD PREAMBLE: END (do not edit)

# Default mode: format (apply changes)
CHECK_MODE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --check)
            CHECK_MODE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--check]"
            echo ""
            echo "Run black formatter on the codebase."
            echo ""
            echo "Options:"
            echo "  --check    Check formatting without making changes (exits non-zero on issues)"
            echo "  -h, --help Show this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

cd "$REPO_DIR"

# Directories to format (contrib/ excluded - external libraries)
DIRS="tolokaforge tests scripts tools"

if [ "$CHECK_MODE" = true ]; then
    log_info "=== Checking black formatting (no changes) ==="
    if ! black --check --diff $DIRS; then
        log_error "❌ Black formatting check failed. Run './scripts/lint/run_black.sh' to fix."
        exit 1
    fi
    log_info "✅ Black formatting check passed!"
else
    log_info "=== Running black formatter ==="
    if ! black $DIRS; then
        log_error "❌ Black formatting failed"
        exit 1
    fi
    log_info "✅ Black formatting completed successfully!"
fi

