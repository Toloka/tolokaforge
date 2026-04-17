#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../scripts/common.sh"

# STANDARD PREAMBLE: END (do not edit)

if [ -z ${1+x} ] ; then
    log_error "Absent argument: {HOST_USER}"
    exit 1
fi
HOST_USER="$1"

if [ -z ${2+x} ] ; then
    log_error "Absent argument: {WORKSPACE_PATH}"
    exit 1
fi
WORKSPACE_PATH="$2"

sudo chown -R "$HOST_USER":"$HOST_USER" "/home/${HOST_USER}/.cache"
# Don't try to change owner for git directories
ls -A "$WORKSPACE_PATH" | egrep -vx '.git(hub)?' | xargs -I{} sudo chown -R "$HOST_USER" "${WORKSPACE_PATH}/{}"

git config --global --add safe.directory "$WORKSPACE_PATH"

# Add GitHub to known_hosts to avoid interactive SSH prompts
log_info "Adding GitHub to SSH known_hosts"
mkdir -p ~/.ssh
ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null || true

# In Codespaces, configure git to use HTTPS instead of SSH for GitHub
if [ "${CODESPACES:-false}" = "true" ]; then
    log_info "Running in Codespaces, configuring git to use HTTPS for GitHub"
    git config --global url."https://github.com/".insteadOf "git@github.com:"
fi

cd "$WORKSPACE_PATH"

# Install pre-commit hooks
log_info "Installing pre-commit hooks"
uv run pre-commit install

# Initialize Git LFS and pull LFS objects (needed for test fixture data)
log_info "Initializing Git LFS"
"${SCRIPT_DIR}/../scripts/setup/init_git_lfs.sh"

uname -a
