#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"

# STANDARD PREAMBLE: END (do not edit)

# =============================================================================
# HOW TO ADD NEW REQUIRED ENVIRONMENT VARIABLES
# =============================================================================
#
# This script was originally designed to set up ANTHROPIC_API_KEY as a required
# environment variable. The ANTHROPIC_API_KEY requirement has been removed to make
# this template more generic, but the structure remains as an example.
#
# To add a new required environment variable (e.g., YOUR_API_KEY):
#
# 1. Update the validate_env_vars() function:
#    Add a check for your variable:
#    ```
#    if [[ -z "${YOUR_API_KEY:-}" ]]; then
#        missing_vars+=("YOUR_API_KEY")
#    fi
#    ```
#
# 2. Update the get_api_key() function (or create a new one):
#    Rename and modify to prompt for your variable:
#    ```
#    get_your_api_key() {
#        log_info "You need to provide YOUR_API_KEY."
#        read -p "Please enter your API key: " YOUR_API_KEY
#        if [[ -z "$YOUR_API_KEY" ]]; then
#            log_error "API key cannot be empty."
#            return 1
#        fi
#        log_info "API key provided."
#        return 0
#    }
#    ```
#
# 3. Update the update_env_file() function:
#    Add your variable to the .env file:
#    ```
#    echo "YOUR_API_KEY=${YOUR_API_KEY}"
#    ```
#
# 4. Update the main logic section:
#    Add a check and call your function:
#    ```
#    if [[ -z "${YOUR_API_KEY:-}" ]]; then
#        if ! get_your_api_key; then
#            exit 1
#        fi
#    fi
#    ```
#
# Example implementation for multiple API keys:
# - Check for ANY required variable in validate_env_vars()
# - Prompt for missing ones in the main logic
# - Update the .env file with all provided variables
#
# =============================================================================

# Check if REPO_DIR is available
if [[ -z "${REPO_DIR+x}" ]]; then
    log_error "REPO_DIR is not set. Cannot determine repository directory."
    exit 1
fi

ENV_FILE="${REPO_DIR}/.env"
log_info "Working with environment file: ${ENV_FILE}"

# Function to validate environment variables
validate_env_vars() {
    local missing_vars=()

    # Check for OPENROUTER_API_KEY
    if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
        missing_vars+=("OPENROUTER_API_KEY")
    fi

    if [[ ${#missing_vars[@]} -eq 0 ]]; then
        log_info "All required environment variables are set."
        return 0
    else
        log_warning "Missing environment variables: ${missing_vars[*]}"
        return 1
    fi
}

# Function to prompt for OpenRouter API key
get_openrouter_api_key() {
    log_info "You need to provide OPENROUTER_API_KEY."
    log_info "You can get your API key from: https://openrouter.ai/keys"
    read -p "Please enter your OpenRouter API key: " OPENROUTER_API_KEY
    if [[ -z "$OPENROUTER_API_KEY" ]]; then
        log_error "API key cannot be empty."
        return 1
    fi
    log_info "API key provided."
    return 0
}

# Function to create or update .env file
update_env_file() {
    log_info "Creating/updating ${ENV_FILE}..."

    # Write environment variables to .env file
    {
        echo "# Environment variables"
        echo "# Generated on $(date)"
        echo ""
        echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
    } > "$ENV_FILE"

    log_info "Environment file updated: ${ENV_FILE}"
}

# Main logic
log_info "Starting environment setup..."

# Step 1: Check if .env exists and try to source it
if [[ -f "$ENV_FILE" ]]; then
    log_info ".env file found. Attempting to source it..."

    # Source the .env file safely
    set -a  # automatically export all variables
    source "$ENV_FILE"
    set +a  # disable automatic export

    log_info "Successfully sourced ${ENV_FILE}"
else
    log_warning ".env file not found at ${ENV_FILE}"
fi

# Step 2: Validate required environment variables
if validate_env_vars; then
    log_info "Environment is properly configured. No action needed."
    exit 0
fi

# Step 3: Handle missing variables
log_info "Setting up missing environment variables..."

# Prompt for OPENROUTER_API_KEY if missing
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    if ! get_openrouter_api_key; then
        exit 1
    fi
fi

# Step 4: Create/update .env file
update_env_file

# Step 5: Final validation
log_info "Performing final validation..."
set -a
source "$ENV_FILE"
set +a

if validate_env_vars; then
    log_info "Environment setup completed successfully!"
    log_info "You can now use the configured environment variables."
else
    log_error "Environment setup failed. Please check the configuration."
    exit 1
fi
