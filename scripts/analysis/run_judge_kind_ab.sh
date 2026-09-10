#!/bin/bash
#
# Drive judge-kind-ab's live kappa-parity + cost A/B against real trial bundles.
#
# Thin wrapper around the `judge-kind-ab` workspace tool that loads the repo's
# .env (provider API keys) via with_env.sh, since a live A/B run makes real
# inference calls. Always exits 0 — a below-threshold kappa is an annotation
# on the rendered report, not a gate failure.
#
# Usage:
#   scripts/analysis/run_judge_kind_ab.sh <bundles-dir> [options]
#
# Example:
#   scripts/analysis/run_judge_kind_ab.sh \
#     /tmp/judge-kind-ab-bundles --model-ref openrouter/openai/gpt-4.1-mini \
#     --kinds single_shot_rubric,chunked_rubric,agentic_rubric \
#     --replays 5 --out-dir /tmp/judge-kind-ab-report

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
REPO_DIR="$( cd "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd )"

exec "${REPO_DIR}/scripts/with_env.sh" uv run judge-kind-ab run "$@"
