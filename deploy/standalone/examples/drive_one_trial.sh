#!/bin/sh
# Drive one real trial through the standalone composed runner stack — any-language
# path. The host needs only Docker (with the compose plugin) and jq; no host
# tolokaforge install. The task is serialised inside the runner container with the
# public tolokaforge.runner.load_task, the start envelope is built with jq, and the
# trial is driven over the `tolokaforge run-trial` JSON-Lines exec wire — the same
# wire the Python driver uses. Prints the graded trial's grade; on a typed trial
# error it prints the error and exits non-zero.
#
# Prerequisites: a running standalone stack (`docker compose up -d --wait` in
# deploy/standalone/) and a provider key in deploy/standalone/.env. The trial reads
# the key from the runner container's environment (compose injects it from .env),
# so this driver forwards no secrets. Override the agent model to match the key you
# set with TOLOKAFORGE_EXAMPLE_PROVIDER / TOLOKAFORGE_EXAMPLE_MODEL.

set -eu
# pipefail is not in POSIX sh (dash lacks it); enable it only where supported so a
# failing `docker compose exec` in a pipeline is not masked by a later stage.
# shellcheck disable=SC3040  # guarded: the subshell probe skips pipefail where unsupported
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
STANDALONE_DIR=$(dirname "$SCRIPT_DIR")
COMPOSE_FILE="$STANDALONE_DIR/docker-compose.yaml"
REPO_ROOT=$(cd "$STANDALONE_DIR/../.." && pwd)

TASK_PACK="$REPO_ROOT/examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01"
PACK_NAME=$(basename "$TASK_PACK")

PROVIDER="${TOLOKAFORGE_EXAMPLE_PROVIDER:-openrouter}"
MODEL="${TOLOKAFORGE_EXAMPLE_MODEL:-anthropic/claude-sonnet-4.6}"

# The shared-stack runtime reaches the executor here; the wire default
# `executor:50051` does not resolve standalone, so point it at the runner's own gRPC.
EXECUTOR_ADDRESS="localhost:50051"

for tool in docker jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'missing required tool: %s (this driver needs docker + jq on PATH)\n' "$tool" >&2
    exit 1
  fi
done

# No `-p`: run from the recipe so the default compose project matches a cold user's
# `docker compose up` in deploy/standalone/.
dc() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# A wire task carries no source directory, so its relative file assets must be
# present in the container: copy the pack in before the trial reads it.
dc cp "$TASK_PACK" runner:/tmp/

# Serialise the task inside the runner so the host needs no tolokaforge.
TASK_JSON=$(dc exec -T -w "/tmp/$PACK_NAME" runner python -c \
  'import json; from tolokaforge.runner import load_task; print(json.dumps(load_task("task.yaml").model_dump(mode="json")))')

# `runtime:"shared"` is load-bearing — `"auto"` would pick the per-trial Docker
# backend, which cannot run inside the composed runner container. `grader` is
# omitted to default to `runner_rpc`.
START=$(jq -cn --argjson task "$TASK_JSON" --arg provider "$PROVIDER" --arg model "$MODEL" \
  '{v: 1, type: "start", task: $task, models: {agent: {provider: $provider, name: $model, temperature: 0, max_tokens: 4096}}, runtime: "shared", conductor: "in_process"}')

set +e
RESULT=$(printf '%s\n' "$START" | dc exec -T -e "EXECUTOR_ADDRESS=$EXECUTOR_ADDRESS" -w "/tmp/$PACK_NAME" runner tolokaforge run-trial)
EXEC_RC=$?
set -e

LINE=$(printf '%s\n' "$RESULT" | grep . | tail -n 1)
if [ -z "$LINE" ]; then
  printf 'run-trial produced no wire output (rc=%s)\n' "$EXEC_RC" >&2
  exit 1
fi

TYPE=$(printf '%s' "$LINE" | jq -r '.type')
if [ "$TYPE" = "error" ]; then
  printf 'trial error [%s]: %s\n' \
    "$(printf '%s' "$LINE" | jq -r '.error_type // "unknown"')" \
    "$(printf '%s' "$LINE" | jq -r '.message // ""')" >&2
  exit 1
fi
if [ "$TYPE" != "result" ]; then
  printf 'unexpected wire envelope type: %s\n' "$LINE" >&2
  exit 1
fi

printf '%s' "$LINE" | jq -r '.result.trajectory.grade'
