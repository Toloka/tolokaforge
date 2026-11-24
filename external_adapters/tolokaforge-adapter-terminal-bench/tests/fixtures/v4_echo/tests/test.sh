#!/usr/bin/env bash
set -uo pipefail
cd /tests
pytest -q test_answer.py
rc=$?

# A v4 verifier records which criteria failed beside the reward and opens both
# to the harness on its way out, which is how the grader's ``detail_paths`` read
# reaches them — that read is unprivileged, so the modes below are load-bearing.
LOGS=/logs/verifier
if [ "$rc" -eq 0 ]; then
  status=passed
  reward=1
else
  status=failed
  reward=0
fi
printf '{"results":{"tests":[{"name":"test_answer_is_42","status":"%s"}]}}\n' \
  "$status" > "$LOGS/ctrf.json"
printf '{"criteria":{"test_answer_is_42":%s}}\n' "$reward" > "$LOGS/reward-details.json"
echo "$reward" > "$LOGS/reward.txt"
chmod 644 "$LOGS/ctrf.json" "$LOGS/reward-details.json" "$LOGS/reward.txt"
chmod 755 "$LOGS"
