#!/bin/bash
set -o pipefail

mkdir -p /logs/verifier

echo "Running tests..."
pytest /work/tests -v 2>&1 | tee /tmp/pytest_output.txt

python3 - << 'PY'
import re
from pathlib import Path

TESTS = [
    "test_factorial_zero",
    "test_factorial_one",
    "test_factorial_two",
    "test_factorial_five",
    "test_factorial_ten",
    "test_factorial_negative_raises",
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
