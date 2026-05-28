#!/bin/bash
set -o pipefail

# Wait for PostgreSQL
for i in $(seq 1 30); do
    PGPASSWORD="${PG_PASSWORD:-seg_pipeline_2024}" psql \
        -h "${PG_HOST:-localhost}" \
        -U "${PG_USER:-airline}" \
        -d "${PG_DB:-airline_db}" \
        -c "SELECT 1" >/dev/null 2>&1 && break
    sleep 2
done

# Run pytest
pytest "${TEST_DIR:-/tests}" -v 2>&1 | tee /tmp/pytest_output.txt

# Calculate reward from test results
python3 - << 'PY'
import re
from pathlib import Path

TESTS = [
    "test_recency_non_negative",
    "test_recency_values_consistent",
    "test_frequency_values_correct",
    "test_frequency_mean_consistent",
    "test_monetary_values_correct",
    "test_monetary_reasonable_range",
    "test_cluster_size_distribution",
    "test_cluster_count",
    "test_cluster_differentiation",
    "test_rfm_features_populated",
    "test_segments_populated",
    "test_segment_report_populated",
    "test_pipeline_log_has_entries",
    "test_segment_report_recency_non_negative",
    "test_rfm_recency_mean_reasonable",
]

output_path = Path("/tmp/pytest_output.txt")
reward_path = Path("/logs/verifier/reward.txt")

text = output_path.read_text(errors="ignore") if output_path.exists() else ""

passed = sum(1 for t in TESTS if re.search(rf"::{re.escape(t)}\s+PASSED", text))
reward = passed / len(TESTS)

reward_path.parent.mkdir(parents=True, exist_ok=True)
reward_path.write_text(f"{reward:.6f}\n")
print(f"\nTests passed: {passed}/{len(TESTS)}  reward={reward:.6f}")
PY

exit 0
