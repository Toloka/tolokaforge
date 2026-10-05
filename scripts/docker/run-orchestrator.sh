#!/usr/bin/env bash
#
# One-command job template for the standalone orchestrator image — run a whole
# ``tolokaforge run`` batch on a clean machine that has only Docker and
# credentials. See docs/ORCHESTRATOR_IMAGE.md for the full walkthrough.
#
# It mounts, into the container:
#   * the host Docker socket  -> /var/run/docker.sock  (Docker-out-of-Docker:
#     the engine shells out to ``docker compose`` against the HOST daemon, so
#     child stacks come up as host siblings)
#   * the run-config directory -> /work/config (read-only)
#   * the task-pack directory  -> /work/tasks  (read-only)
#   * an output directory      -> /work/out    (read-write)
#   * the .env file            -> /work/.env   (read-only; DotEnvProvider reads it)
# and runs ``tolokaforge run --config /work/config/<config>``.
#
# Secrets: this wrapper forwards NOTHING secret itself. Credentials come only
# from the mounted read-only .env (or a ``-e TOLOKAFORGE_SECRETS_JSON`` you add
# via EXTRA_DOCKER_ARGS). Point your run config's ``evaluation.output_dir`` at
# ``/work/out`` and its project paths under ``/work/tasks`` so reports land in
# the mounted output directory and tasks resolve inside the container.
#
# Usage:
#   scripts/docker/run-orchestrator.sh -c CONFIG [options] [-- EXTRA run args]
#
#   -c CONFIG     Host path to the run-config YAML (required).
#   -i IMAGE      Orchestrator image to run (default: tolokaforge-orchestrator:local).
#   -t TASKS_DIR  Host path to the task-pack directory, mounted read-only at
#                 /work/tasks (default: ./tasks).
#   -o OUT_DIR    Host path for outputs, mounted read-write at /work/out
#                 (default: ./out). Created if absent.
#   -e ENV_FILE   Host path to the .env file, mounted read-only at /work/.env
#                 (default: ./.env). A keyless --dry-run needs none.
#   -h            Show this help and exit.
#
# Anything after ``--`` is forwarded verbatim to ``tolokaforge run`` (e.g.
# ``-- --dry-run`` or ``-- --cost-limit 5``).
set -euo pipefail

IMAGE="tolokaforge-orchestrator:local"
CONFIG=""
TASKS_DIR="./tasks"
OUT_DIR="./out"
ENV_FILE="./.env"

usage() {
    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
}

while getopts ":c:i:t:o:e:h" opt; do
    case "${opt}" in
        c) CONFIG="${OPTARG}" ;;
        i) IMAGE="${OPTARG}" ;;
        t) TASKS_DIR="${OPTARG}" ;;
        o) OUT_DIR="${OPTARG}" ;;
        e) ENV_FILE="${OPTARG}" ;;
        h) usage; exit 0 ;;
        :) echo "error: -${OPTARG} needs an argument" >&2; exit 2 ;;
        \?) echo "error: unknown option -${OPTARG}" >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

if [[ -z "${CONFIG}" ]]; then
    echo "error: -c CONFIG is required" >&2
    usage >&2
    exit 2
fi
if [[ ! -f "${CONFIG}" ]]; then
    echo "error: run config not found: ${CONFIG}" >&2
    exit 1
fi

# Resolve the config to an absolute dir + basename so Docker can bind-mount the
# directory and the CLI gets a path inside the container.
CONFIG_DIR="$(cd "$(dirname "${CONFIG}")" && pwd)"
CONFIG_NAME="$(basename "${CONFIG}")"

# The output directory is created on the host so the bind mount is a directory
# (not a file) and the reports are owned where the operator can read them.
mkdir -p "${OUT_DIR}"
OUT_DIR_ABS="$(cd "${OUT_DIR}" && pwd)"

DOCKER_ARGS=(
    run --rm
    -v "/var/run/docker.sock:/var/run/docker.sock"
    -v "${CONFIG_DIR}:/work/config:ro"
    -v "${OUT_DIR_ABS}:/work/out"
)

# The socket's group must be added so the non-root ``runner`` user can reach the
# mounted daemon socket. On Linux the gid is the socket's group owner. On Docker
# Desktop (macOS/Windows) the socket is proxied and group-ownership differs — a
# ``--group-add`` is usually unnecessary there; drop it if the run errors.
if [[ -S /var/run/docker.sock ]]; then
    SOCK_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null || true)"
    if [[ -n "${SOCK_GID}" ]]; then
        DOCKER_ARGS+=(--group-add "${SOCK_GID}")
    fi
fi

# Task pack: mounted read-only when the directory exists. A --dry-run against an
# in-image / in-config example may not need one.
if [[ -d "${TASKS_DIR}" ]]; then
    TASKS_DIR_ABS="$(cd "${TASKS_DIR}" && pwd)"
    DOCKER_ARGS+=(-v "${TASKS_DIR_ABS}:/work/tasks:ro")
fi

# .env: mounted read-only when present. DotEnvProvider reads /work/.env inside
# the container. Absent is fine for a keyless --dry-run.
if [[ -f "${ENV_FILE}" ]]; then
    ENV_FILE_ABS="$(cd "$(dirname "${ENV_FILE}")" && pwd)/$(basename "${ENV_FILE}")"
    DOCKER_ARGS+=(-v "${ENV_FILE_ABS}:/work/.env:ro")
fi

# Anything the operator appends in EXTRA_DOCKER_ARGS (e.g. -e
# TOLOKAFORGE_SECRETS_JSON=..., --network host) is spliced in before the image.
if [[ -n "${EXTRA_DOCKER_ARGS:-}" ]]; then
    # shellcheck disable=SC2206  # intentional word-splitting of operator-supplied flags
    EXTRA=(${EXTRA_DOCKER_ARGS})
    DOCKER_ARGS+=("${EXTRA[@]}")
fi

exec docker "${DOCKER_ARGS[@]}" "${IMAGE}" \
    run --config "/work/config/${CONFIG_NAME}" "$@"
