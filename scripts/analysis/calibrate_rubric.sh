#!/bin/bash
#
# Calibrate a rubric judge against golden fixtures and apply the trust gate.
#
# Thin wrapper around the `rubric-calibrator` workspace tool that loads the
# repo's .env (provider API keys) via with_env.sh, since calibration runs real
# inference. Exits non-zero when the trust gate fails (agreement below
# threshold or any fixture errored).
#
# Usage:
#   scripts/analysis/calibrate_rubric.sh <fixtures...> [--model-ref ...] [--threshold ...]
#
# Example:
#   scripts/analysis/calibrate_rubric.sh \
#     tools/rubric-calibrator/fixtures --threshold 0.6

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
REPO_DIR="$( cd "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd )"

exec "${REPO_DIR}/scripts/with_env.sh" uv run rubric-calibrator "$@"
