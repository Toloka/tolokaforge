#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../scripts/common.sh"

# STANDARD PREAMBLE: END (do not edit)

git config --global core.editor "code --wait"

"${REPO_DIR}/scripts/setup/create_python_venv.sh"
