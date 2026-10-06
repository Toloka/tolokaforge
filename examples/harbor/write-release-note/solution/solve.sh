#!/usr/bin/env bash
# Reference solution: write the exact release note the verifier checks. Harbor's
# keyless `oracle` agent runs this script in place of a model, so a run with
# `agent: oracle` reaches the verifier with the task already solved (reward 1.0).
set -euo pipefail

printf '%s\n' 'harbor release: ready' > /app/RELEASE_NOTE.txt
