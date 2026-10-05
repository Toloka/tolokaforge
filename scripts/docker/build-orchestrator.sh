#!/usr/bin/env bash
#
# Build the standalone orchestrator image — the full ``tolokaforge run`` batch
# driver (see docs/ORCHESTRATOR_IMAGE.md).
#
# Builds the engine wheel on the host with ``uv build``, assembles a minimal
# build context (the Dockerfile, the wheel, and the ``tolokaforge_models``
# sibling source the in-container stage compiles), and runs ``docker build``
# with the right build-args. The temp context sidesteps the repo .dockerignore
# that excludes ``dist/`` — mirroring how ``tolokaforge docker build`` stages
# its own wheel context.
#
# Usage:
#   scripts/docker/build-orchestrator.sh [-t TAG] [-e EXTRAS]
#
#   -t TAG      Image tag to build (default: tolokaforge-orchestrator:local)
#   -e EXTRAS   Wheel extras to install (default: dx). The engine needs ``dx``;
#               ``dx,adapters`` adds the opt-in adapter packages (those resolve
#               from their own distributions — see docs/ORCHESTRATOR_IMAGE.md).
#   -h          Show this help and exit.
#
# Secrets are NOT a build input: nothing here reads a key. They reach a running
# container only via a mounted .env or a runtime env var — see
# scripts/docker/run-orchestrator.sh.
set -euo pipefail

IMAGE_TAG="tolokaforge-orchestrator:local"
EXTRAS="dx"

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while getopts ":t:e:h" opt; do
    case "${opt}" in
        t) IMAGE_TAG="${OPTARG}" ;;
        e) EXTRAS="${OPTARG}" ;;
        h) usage; exit 0 ;;
        :) echo "error: -${OPTARG} needs an argument" >&2; exit 2 ;;
        \?) echo "error: unknown option -${OPTARG}" >&2; exit 2 ;;
    esac
done

# Resolve the repo root from this script's location so the build works from any
# working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKERFILE="${REPO_ROOT}/tolokaforge/docker/dockerfiles/orchestrator.Dockerfile"

cd "${REPO_ROOT}"

echo "==> Building engine wheel with uv build"
WHEEL_OUT="$(mktemp -d)"
trap 'rm -rf "${WHEEL_OUT}" "${BUILD_CTX:-}"' EXIT
uv build --wheel --out-dir "${WHEEL_OUT}"

shopt -s nullglob
WHEELS=("${WHEEL_OUT}"/tolokaforge-*.whl)
shopt -u nullglob
if [[ ${#WHEELS[@]} -eq 0 ]]; then
    echo "error: uv build produced no tolokaforge-*.whl in ${WHEEL_OUT}" >&2
    exit 1
fi
WHEEL_PATH="${WHEELS[0]}"
WHEEL_FILENAME="$(basename "${WHEEL_PATH}")"
echo "    wheel: ${WHEEL_FILENAME}"

echo "==> Assembling build context"
BUILD_CTX="$(mktemp -d)"
cp "${DOCKERFILE}" "${BUILD_CTX}/orchestrator.Dockerfile"
cp "${WHEEL_PATH}" "${BUILD_CTX}/${WHEEL_FILENAME}"
cp -R "${REPO_ROOT}/tolokaforge_models" "${BUILD_CTX}/tolokaforge_models"

echo "==> docker build ${IMAGE_TAG} (extras: ${EXTRAS})"
DOCKER_BUILDKIT=1 docker build \
    -f "${BUILD_CTX}/orchestrator.Dockerfile" \
    --build-arg "PYTHON_VERSION=$(cat "${REPO_ROOT}/.python-version")" \
    --build-arg "WHEEL_FILENAME=${WHEEL_FILENAME}" \
    --build-arg "EXTRAS=${EXTRAS}" \
    -t "${IMAGE_TAG}" \
    "${BUILD_CTX}"

echo "==> Built ${IMAGE_TAG}"
echo "    Smoke it:   docker run --rm ${IMAGE_TAG} --help"
echo "    Run a job:  scripts/docker/run-orchestrator.sh -i ${IMAGE_TAG} ..."
