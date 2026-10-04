#!/usr/bin/env bash
# Harbor verifier: score the trial by whether the required release note exists
# with the exact expected contents, then write the reward Harbor reads back
# from /logs/verifier/reward.txt.
set -uo pipefail

reward_dir=/logs/verifier
mkdir -p "$reward_dir"

target=/app/RELEASE_NOTE.txt
expected=$'harbor release: ready'

reward=0.0
if [ -f "$target" ] && [ "$(cat "$target")" = "$expected" ]; then
    reward=1.0
fi

printf '%s\n' "$reward" > "$reward_dir/reward.txt"
exit 0
