#!/usr/bin/env bash
set -uo pipefail
cd /tests
pytest -q test_answer.py
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
