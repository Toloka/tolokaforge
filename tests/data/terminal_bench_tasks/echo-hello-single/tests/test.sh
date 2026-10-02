#!/bin/bash
set -o pipefail

mkdir -p /logs/verifier
if grep -qx hello /app/greeting.txt 2>/dev/null; then
    echo 1.0 > /logs/verifier/reward.txt
else
    echo 0.0 > /logs/verifier/reward.txt
fi
