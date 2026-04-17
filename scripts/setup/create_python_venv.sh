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

# Sync Python dependencies with uv
# Use --index-strategy unsafe-best-match to allow PyPI fallback when Azure DevOps
# doesn't have platform-specific wheels (e.g., macOS ARM64)
log_info "Syncing Python dependencies with uv..."
if ! uv sync --index-strategy unsafe-best-match 2>&1 | tee /tmp/uv_sync_output.log; then
    # Check if the error is about missing platform wheels
    if grep -q "doesn't have.*wheel for the current platform\|only has wheels for" /tmp/uv_sync_output.log; then
        log_warning "Some packages don't have wheels for this platform in Azure DevOps"
        log_info "Regenerating lock file to use PyPI fallback for platform-specific packages..."
        # Remove lock file to force fresh resolution from all indexes
        LOCK_BACKUP="${REPO_DIR}/uv.lock.backup"
        if [ -f "${REPO_DIR}/uv.lock" ]; then
            cp "${REPO_DIR}/uv.lock" "$LOCK_BACKUP"
            rm "${REPO_DIR}/uv.lock"
            log_info "Backed up existing lock file"
        fi
        if uv lock --index-strategy unsafe-best-match; then
            log_info "Lock file regenerated, retrying sync..."
            if uv sync --index-strategy unsafe-best-match; then
                log_info "Successfully synced dependencies"
                rm -f "$LOCK_BACKUP" /tmp/uv_sync_output.log
            else
                log_error "Sync failed after regenerating lock file"
                if [ -f "$LOCK_BACKUP" ]; then
                    mv "$LOCK_BACKUP" "${REPO_DIR}/uv.lock"
                    log_info "Restored original lock file"
                fi
                rm -f /tmp/uv_sync_output.log
                exit 1
            fi
        else
            log_error "Failed to regenerate lock file. Please check your network connection and Azure DevOps authentication."
            if [ -f "$LOCK_BACKUP" ]; then
                mv "$LOCK_BACKUP" "${REPO_DIR}/uv.lock"
                log_info "Restored original lock file"
            fi
            rm -f /tmp/uv_sync_output.log
            exit 1
        fi
    else
        log_error "uv sync failed for unknown reason. Check the error messages above."
        rm -f /tmp/uv_sync_output.log
        exit 1
    fi
fi
rm -f /tmp/uv_sync_output.log

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
#   scripts/release/build_docker_images.sh
# Tests that need Docker images will skip gracefully if not built.

log_info "Development environment setup complete!"
log_info ""
log_info "Run tests with: scripts/with_env.sh uv run pytest"
log_info "Build Docker images (optional): scripts/release/build_docker_images.sh"
