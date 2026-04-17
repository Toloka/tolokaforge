#!/usr/bin/env bash
# Tolokaforge Smoke Tests — minimal must-pass gate between refactoring stages.
# See: plans/stage0-pre-refactoring-stabilisation.md (Task 0.3)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh

# Load .env secrets (API keys, etc.)
load_env_file "${ROOT_DIR}/.env"

FAILED=0
TIER_PASS=0
TIER_FAIL=0

run_tier() {
    local tier_name="$1"
    shift
    echo ""
    echo "--- ${tier_name} ---"
    if uv run pytest "$@" -v --tb=short; then
        TIER_PASS=$((TIER_PASS + 1))
        echo "  ✓ ${tier_name} PASSED"
    else
        FAILED=1
        TIER_FAIL=$((TIER_FAIL + 1))
        echo "  ✗ ${tier_name} FAILED"
    fi
}

echo "=== Tolokaforge Smoke Tests ==="
echo "Running from: $ROOT_DIR"
echo "Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Tier 1: Core adapters — adapter factory, NativeAdapter
run_tier "Tier 1: Adapter contracts" \
    tests/unit/test_adapters.py::TestGetAdapter \
    tests/unit/test_adapters.py::TestNativeAdapter

# Tier 2: Pipeline components — metrics, resume, grading primitives
run_tier "Tier 2: Pipeline components" \
    tests/unit/test_metrics.py \
    tests/unit/test_resume.py \
    tests/unit/grading/test_state_checks.py \
    tests/unit/grading/test_transcript.py

# Tier 3: Functional smoke — browser tool contract, evaluators, config parsing
run_tier "Tier 3: Functional smoke" \
    tests/unit/test_browser_tool.py::test_browser_schema \
    tests/unit/grading/test_evaluators.py \
    tests/unit/test_adapters.py::TestAdapterConfigParsing

# Tier 4: Docker health (conditional — only if Docker daemon is reachable)
echo ""
echo "--- Tier 4: Docker health ---"
if docker info >/dev/null 2>&1; then
    run_tier "Tier 4: Docker health" \
        tests/integration/docker/test_docker_integration.py
else
    echo "  Docker not available — skipping Tier 4"
fi

echo ""
echo "==============================="
echo "Tiers passed: ${TIER_PASS}"
echo "Tiers failed: ${TIER_FAIL}"
echo "==============================="

if [ "$FAILED" -eq 0 ]; then
    echo "=== SMOKE PASSED ==="
    exit 0
else
    echo "=== SMOKE FAILED ==="
    exit 1
fi
