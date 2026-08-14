#!/bin/bash
set -o pipefail

pytest $TEST_DIR -v 2>&1 | tee /tmp/pytest_output.txt

python3 - << 'PY'
import re
from pathlib import Path

TESTS = ["test_hello_file_exists", "test_hello_file_content"]

output_path = Path("/tmp/pytest_output.txt")
reward_path = Path("/logs/verifier/reward.txt")

text = output_path.read_text(errors="ignore") if output_path.exists() else ""

passed = sum(1 for t in TESTS if re.search(rf"::{re.escape(t)}\s+PASSED", text))
reward = passed / len(TESTS)

reward_path.parent.mkdir(parents=True, exist_ok=True)
reward_path.write_text(f"{reward:.6f}\n")
PY

exit 0
