#!/bin/bash
# Initialize Git LFS and pull LFS objects.
#
# Usage: scripts/init_git_lfs.sh
#
# Some functional and golden-set tests depend on Git LFS data under
# tests/data/projects/.  Missing LFS content causes fixture/data failures
# (Known Gotcha #2 in AGENTS.md).

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"

# Check if git-lfs is installed
if ! command -v git-lfs &>/dev/null; then
    log_info "Installing git-lfs..."
    sudo apt-get update -qq && sudo apt-get install -y -qq git-lfs 2>/dev/null \
        || log_warn "Could not install git-lfs via apt. Trying GitHub release..."
    if ! command -v git-lfs &>/dev/null; then
        log_error "git-lfs is not available and could not be installed."
        exit 1
    fi
fi

log_info "Initializing Git LFS..."
git lfs install --local 2>/dev/null || git lfs install

log_info "Pulling Git LFS objects..."
git lfs pull

log_info "Git LFS initialization complete."
