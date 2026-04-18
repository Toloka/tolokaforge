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

# Fix Docker credential store if the configured helper is broken.
# Devcontainer environments sometimes set a credsStore that references
# docker-credential-secretservice which may not be available, causing
# all Docker SDK operations (build, pull, images.list) to fail.
if [ -f "$HOME/.docker/config.json" ]; then
    CREDS_STORE=$(python3 -c "import json; c=json.load(open('$HOME/.docker/config.json')); print(c.get('credsStore',''))" 2>/dev/null || echo "")
    if [ -n "$CREDS_STORE" ]; then
        HELPER="docker-credential-${CREDS_STORE}"
        if ! command -v "$HELPER" >/dev/null 2>&1 || ! "$HELPER" list >/dev/null 2>&1; then
            log_warning "Docker credential helper '$HELPER' is not functional — removing credsStore from config"
            python3 -c "
import json, pathlib
p = pathlib.Path('$HOME/.docker/config.json')
c = json.loads(p.read_text())
c.pop('credsStore', None)
p.write_text(json.dumps(c, indent=2) + '\n')
"
        fi
    fi
fi

cd "$WORKSPACE_PATH"

# Install pre-commit hooks
log_info "Installing pre-commit hooks"
uv run pre-commit install

# Initialize Git LFS and pull LFS objects (needed for test fixture data)
log_info "Initializing Git LFS"
"${SCRIPT_DIR}/../scripts/setup/init_git_lfs.sh"

uname -a
