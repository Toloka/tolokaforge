#!/bin/bash
set -euo pipefail

# Both probes sit in the `if` condition, where `set -e` does not fire — a missing
# artifact has to reach the else branch and be scored zero, not abort the suite
# before it writes a reward at all.
mkdir -p /logs/verifier
if grep -qx hello /app/answer.txt 2>/dev/null && grep -q hello /dumps/greeting.sql 2>/dev/null; then
    echo 1.0 > /logs/verifier/reward.txt
else
    echo 0.0 > /logs/verifier/reward.txt
fi
