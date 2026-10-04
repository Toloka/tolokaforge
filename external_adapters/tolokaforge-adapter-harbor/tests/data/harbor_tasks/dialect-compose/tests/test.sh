#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
reward=0.0
[ -f /app/done.txt ] && reward=1.0
printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
