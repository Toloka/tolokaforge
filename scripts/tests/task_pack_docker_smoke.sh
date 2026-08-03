#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

PACK_A="$TMP_DIR/pack_a"
PACK_B="$TMP_DIR/pack_b"
mkdir -p "$PACK_A/tasks/browser/public_a" "$PACK_B/tasks/mobile/public_b"

cat > "$PACK_A/tasks/browser/public_a/task.yaml" <<'YAML'
task_id: "public_a"
name: "public_a"
category: "browser"
description: "docker smoke task"
initial_state:
  json_db: null
tools:
  agent:
    enabled: []
  user:
    enabled: []
user_simulator:
  mode: "scripted"
  persona: "cooperative"
grading: "grading.yaml"
YAML

# Non-scoring by design: this smoke exercises task-pack mounting, not grading. An
# empty weight map beside no component section asks for nothing, which is the one
# shape a pack may declare and still pass on nothing.
cat > "$PACK_A/tasks/browser/public_a/grading.yaml" <<'YAML'
combine:
  method: weighted
  weights: {}
  pass_threshold: 1.0
YAML

cat > "$PACK_B/tasks/mobile/public_b/task.yaml" <<'YAML'
task_id: "public_b"
name: "public_b"
category: "mobile"
description: "docker smoke task"
initial_state:
  json_db: null
tools:
  agent:
    enabled: []
  user:
    enabled: []
user_simulator:
  mode: "scripted"
  persona: "cooperative"
grading: "grading.yaml"
YAML

cat > "$PACK_B/tasks/mobile/public_b/grading.yaml" <<'YAML'
combine:
  method: weighted
  weights: {}
  pass_threshold: 1.0
YAML

CONFIG_PATH="$TMP_DIR/run_taskpacks_smoke.yaml"
OVERRIDE_PATH="$TMP_DIR/docker-compose.taskpacks.override.yaml"

cat > "$CONFIG_PATH" <<YAML
models:
  agent:
    provider: mock
    name: mock-agent
orchestrator:
  workers: 1
  repeats: 1
evaluation:
  task_packs:
    - "$PACK_A"
    - "$PACK_B"
  tasks_glob: "**/task.yaml"
  output_dir: "output/task_pack_docker_smoke"
YAML

uv run python scripts/generate_task_pack_compose_override.py \
  --config "$CONFIG_PATH" \
  --output "$OVERRIDE_PATH"

uv run python - <<'PY' "$OVERRIDE_PATH"
from pathlib import Path
import sys
import yaml

override_path = Path(sys.argv[1])
data = yaml.safe_load(override_path.read_text())
services = data.get("services", {})

runner = services.get("runner", {})

runner_env = runner.get("environment", {})

assert "TASKS_DIRS" in runner_env, "Missing TASKS_DIRS in runner env"
assert runner_env["TASKS_DIRS"] == "/app/tasks,/taskpacks/0,/taskpacks/1"
assert "TASK_PACKS_DIRS" in runner_env, "Missing TASK_PACKS_DIRS in runner env"
assert runner_env["TASK_PACKS_DIRS"] == "/taskpacks/0,/taskpacks/1"

runner_volumes = runner.get("volumes", [])
assert len(runner_volumes) == 2, "Expected two task-pack mounts for runner"
print("Override content validated.")
PY

# Merge with base docker-compose.yaml if it exists (deleted in Phase 1 Stage 2,
# replaced by Python ServiceStack — but keep check for future re-introduction)
if [ -f docker-compose.yaml ]; then
  docker compose -f docker-compose.yaml -f "$OVERRIDE_PATH" config > "$TMP_DIR/merged.compose.yaml"
fi
echo "Docker task-pack compose smoke passed."
