#!/bin/bash
set -o pipefail

mkdir -p /logs/verifier
if psql -h db -U greeter -d greeter -tAc "SELECT 1 FROM greeting" 2>/dev/null | grep -q 1; then
    echo 1.0 > /logs/verifier/reward.txt
else
    echo 0.0 > /logs/verifier/reward.txt
fi
