#!/bin/bash
set -o pipefail

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
for i in $(seq 1 30); do
    PGPASSWORD="${PG_PASSWORD:-billing}" psql \
        -h "${PG_HOST:-localhost}" \
        -U "${PG_USER:-billing}" \
        -d "${PG_DB:-billing_db}" \
        -c "SELECT 1" >/dev/null 2>&1 && break
    sleep 2
done

# Wait for the billing service
echo "Waiting for billing service..."
for i in $(seq 1 30); do
    curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
    sleep 2
done

# Run pytest
echo "Running tests..."
pytest "${TEST_DIR:-/tests}" -v 2>&1 | tee /tmp/pytest_output.txt

python3 - << 'PY'
import re
from pathlib import Path

TESTS = [
    "test_health_endpoint",
    "test_percentage_fee_is_fixed",
    "test_percentage_fee_large_amount",
    "test_tiered_fee_small_amount",
    "test_tiered_fee_medium_amount",
    "test_tiered_fee_large_amount",
    "test_flat_rate_fee_correct",
    "test_hold3_fee_corrected",
    "test_hold4_fee_corrected",
    "test_hold5_fee_unchanged",
    "test_hold6_fee_corrected",
    "test_pre_bug_holds_unchanged",
    "test_transactions_corrected_for_hold3",
    "test_transactions_corrected_for_hold4",
    "test_transactions_unchanged_for_hold5",
    "test_transactions_corrected_for_hold6",
    "test_reports_api_accessible",
    "test_reports_bug_period_fees_corrected",
    "test_reports_alice_corp_total_fees",
    "test_report_summaries_match_holds",
    "test_client_fee_totals_alice",
    "test_client_fee_totals_bob",
    "test_client_fee_totals_charlie",
    "test_billing_ledger_hold3_net_fee",
    "test_billing_ledger_hold4_net_fee",
    "test_billing_ledger_hold5_net_fee",
    "test_billing_ledger_hold6_net_fee",
    "test_billing_ledger_pre_bug_unchanged",
    "test_cancel_hold",
    "test_get_hold",
]

output_path = Path("/tmp/pytest_output.txt")
reward_path = Path("/logs/verifier/reward.txt")

text = output_path.read_text(errors="ignore") if output_path.exists() else ""

passed = sum(1 for t in TESTS if re.search(rf"::{re.escape(t)}\s+PASSED", text))
reward = passed / len(TESTS)

reward_path.parent.mkdir(parents=True, exist_ok=True)
reward_path.write_text(f"{reward:.6f}\n")
print(f"Tests passed: {passed}/{len(TESTS)}  reward={reward:.6f}")
PY

exit 0
