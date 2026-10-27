#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"

# STANDARD PREAMBLE: END (do not edit)

# Configure git editor
git config --global core.editor "code --wait"

# Check for uv package manager
check_installed uv "Please install uv: https://docs.astral.sh/uv/getting-started/installation/"

cd "$REPO_DIR"

# Sync Python dependencies to exactly the versions in uv.lock
log_info "Syncing Python dependencies from uv.lock..."
uv sync --locked

# Install playwright system dependencies (required for browser tests)
log_info "Installing Playwright system dependencies..."
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux: use apt-get
    if command -v sudo &> /dev/null; then
        PLAYWRIGHT_PATH=$(uv run which playwright 2>/dev/null || echo "")
        if [ -n "$PLAYWRIGHT_PATH" ]; then
            sudo "$PLAYWRIGHT_PATH" install-deps || log_warning "Failed to install Playwright system dependencies"
        else
            sudo apt-get update -qq && sudo apt-get install -y -qq libcairo2 libpango-1.0-0 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 || log_warning "Failed to install Playwright dependencies via apt-get"
        fi
    else
        log_warning "sudo not available - skipping Playwright system dependency install"
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS: playwright install-deps handles it automatically
    uv run playwright install-deps chromium || log_warning "Failed to install Playwright system dependencies"
else
    log_warning "Unknown OS type - skipping Playwright system dependency install"
fi

log_info "Installing Playwright browsers..."
if uv run playwright install chromium; then
    log_info "Playwright browsers installed successfully"
else
    log_warning "Failed to install Playwright browsers - browser tests may fail"
fi

# NOTE: Docker images are NOT built during environment setup to keep
# Codespaces startup fast and save disk space (~1.5-2GB).
# To build Docker images for integration tests, run:
#   make docker-build
# Tests that need Docker images will skip gracefully if not built.

log_info "Development environment setup complete!"
log_info ""
log_info "Run tests with: scripts/with_env.sh uv run pytest"
log_info "Build Docker images (optional): make docker-build"
