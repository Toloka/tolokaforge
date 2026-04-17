#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"

# STANDARD PREAMBLE: END (do not edit)

# Default mode: fix (apply changes)
CHECK_MODE=false
FORMAT_ONLY=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --check)
            CHECK_MODE=true
            shift
            ;;
        --format-only)
            FORMAT_ONLY=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--check] [--format-only]"
            echo ""
            echo "Run ruff linter and formatter on the codebase."
            echo ""
            echo "Options:"
            echo "  --check       Check only, no auto-fix (exits non-zero on issues)"
            echo "  --format-only Only run ruff format, skip linting"
            echo "  -h, --help    Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0            # Fix issues and format"
            echo "  $0 --check    # Check only (for CI)"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

cd "$REPO_DIR"

# Directories to lint/format (contrib/ excluded - external libraries)
DIRS="tolokaforge tests scripts tools"

overall_success=true

if [ "$FORMAT_ONLY" = false ]; then
    if [ "$CHECK_MODE" = true ]; then
        log_info "=== Checking ruff linting (no fix) ==="
        if ! ruff check $DIRS; then
            log_error "❌ Ruff check failed. Run './scripts/lint/run_ruff.sh' to fix auto-fixable issues."
            overall_success=false
        else
            log_info "✅ Ruff linting check passed!"
        fi
    else
        log_info "=== Running ruff check with auto-fix ==="
        if ! ruff check --fix $DIRS; then
            log_error "❌ Ruff check failed (some issues may not be auto-fixable)"
            overall_success=false
        else
            log_info "✅ Ruff check completed!"
        fi
    fi
fi

if [ "$CHECK_MODE" = true ]; then
    log_info "=== Checking ruff formatting (no changes) ==="
    if ! ruff format --check $DIRS; then
        log_error "❌ Ruff format check failed. Run './scripts/lint/run_ruff.sh' to fix."
        overall_success=false
    else
        log_info "✅ Ruff format check passed!"
    fi
else
    log_info "=== Running ruff formatter ==="
    if ! ruff format $DIRS; then
        log_error "❌ Ruff format failed"
        overall_success=false
    else
        log_info "✅ Ruff format completed!"
    fi
fi

# Final status
if [ "$overall_success" = true ]; then
    log_info "🎉 All ruff operations completed successfully!"
else
    log_error "❌ Some ruff operations failed. Please check the output above."
    exit 1
fi
