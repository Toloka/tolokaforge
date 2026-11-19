#!/usr/bin/env bash
set -euo pipefail

echo hello > /app/answer.txt
psql -h db -U greeter -d greeter -c \
    "CREATE TABLE IF NOT EXISTS greeting (word text); INSERT INTO greeting VALUES ('hello');"
