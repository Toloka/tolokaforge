#!/bin/bash
# Reclaim Docker disk during a long matrix run.
#
# Reserved for use BETWEEN rows in a matrix loop — never mid-row. A
# per-trial container that is currently up owns its layer set and
# `docker system prune` will refuse to touch it, so this is safe to
# run at any time, but the reclaim only pays off between rows.
#
# What it prunes:
#   - dangling build layers older than 1 hour (harness-image builds
#     rebuild the layer on every task, and the intermediate layers
#     age out fast)
#   - stopped containers whose exit is older than 48 hours (per-trial
#     stacks that tore down cleanly leave the base image behind and
#     no live container to hold the layer)
#   - image layers not referenced by any running container, older
#     than 48 hours (matches the `--filter until=48h` semantics
#     Docker uses to keep the "last cycle" intact)
#
# What it does NOT prune:
#   - volumes (task-fixture data lives there)
#   - the local image registry `tolokaforge/docker/registry.py`
#     owns — that has its own `.prune(keep_latest=3)` API the matrix
#     loop should call in Python
set -euo pipefail

echo "[prune] before:"
docker system df 2>&1 | head -5

docker builder prune --filter until=1h --force >/dev/null 2>&1 || true
docker container prune --filter until=48h --force >/dev/null 2>&1 || true
docker image prune --filter until=48h --force --all >/dev/null 2>&1 || true

echo "[prune] after:"
docker system df 2>&1 | head -5
