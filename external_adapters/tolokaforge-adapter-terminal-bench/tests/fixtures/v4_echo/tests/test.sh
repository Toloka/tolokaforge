#!/usr/bin/env bash
set -uo pipefail
cd /tests
pytest -q test_answer.py
rc=$?

# A v4 verifier records which criteria failed beside the reward. The mode below
# seals each one to the user the task grades as, the way a pack that keeps its
# verdict out of the agent's reach does — so the grader's ``detail_paths`` read
# only lands if it runs as that user too. The seal is on the files and not on
# $LOGS, which is a host bind mount: a root-owned 0700 directory there would
# also stop the host unpicking the trial's staging tree at teardown.
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
chmod 600 "$LOGS/ctrf.json" "$LOGS/reward-details.json" "$LOGS/reward.txt"
