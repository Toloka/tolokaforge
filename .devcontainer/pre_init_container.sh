#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../scripts/common.sh"

# STANDARD PREAMBLE: END (do not edit)

log_info "Pre-init: begin"

if [[ "${CODESPACES:-false}" = "true" ]]; then
    log_warning "In a Codespaces. Do nothing"
    log_info "Pre-init step is skipped!"
    exit 0
fi

if command -v ssh-add >/dev/null 2>&1; then
    if ! RESULT=$(ssh-add -l 2>&1); then
        log_warning "ssh agent unavailable: $RESULT"
        log_info "Continuing without host SSH agent."
        log_info "Generate a new SSH key: https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent#generating-a-new-ssh-key"
        log_info "Add the SSH key to the ssh-agent: https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent#adding-your-ssh-key-to-the-ssh-agent"
    fi
else
    log_warning "ssh-add is not installed on host. Continuing without SSH agent check."
fi

log_info "Pre-init step is success!"
