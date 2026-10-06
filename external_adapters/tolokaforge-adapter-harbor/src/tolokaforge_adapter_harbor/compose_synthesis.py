"""Synthesise the two compose files a Harbor trial provisions.

The engine runs Harbor by bringing up an agent container that has the
``harbor`` harness + the Docker CLI + the TB2 task pack baked in, exec-ing
``harbor run`` once inside it (the "agent" step), then grading from the reward a
generated ``tests/test.sh`` extracts from Harbor's native ``result.json``. This
module materialises that container's build context and emits the two-stack
composition plan (ADR-0044): a run-scope **engine** stack (runner + db-service)
and a trial-scope **task** stack (the Harbor agent service).

The Harbor-specific deviation from the inspect synthesis is Docker-out-of-Docker
(DooD): ``harbor run`` shells ``docker compose`` to build Harbor's own task
sandbox, so the agent service carries the Docker CLI **and** a bind mount of the
host Docker socket. The socket mount is declared directly on the synthesised
task-stack service; task-stack composes an adapter writes are not re-validated by
the manifest's relative-bind-mount safety check (only the engine mirror compose
is), which is the same freedom the inspect agent uses to declare its own volumes.

No subprocess is run here; the image is built declaratively by the orchestrator
from the ``build:`` context this module writes.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.compose_materialisation import DOCKER_SOCKET_PATH
from tolokaforge.runner.models import _FLOATING_IMAGE_TAGS

PROJECT_PREFIX = "tf_harbor_"
CONTAINER_LOGS_DIR = "/logs"
HARBOR_JOBS_DIR = "/logs/harbor"
HARBOR_JOB_NAME = "trial"
STAGING_LOGS_DIRNAME = "_logs"
TASK_PACK_DIRNAME = "task"
CONTAINER_TASK_DIR = "/app/task"

_TASK_COMPOSE_FILENAME = "docker-compose.tolokaforge.yaml"
_ENGINE_COMPOSE_FILENAME = "docker-compose.engine.yaml"
_ENGINE_STAGING_DIRNAME = "engine"
_TRIAL_SLUG = "${TOLOKAFORGE_TRIAL_SLUG}"
_RUNNER_SERVICE = "runner"
_DB_SERVICE = "db-service"

# Docker CLI + compose plugin, so ``harbor run -e docker`` can shell
# ``docker compose`` against the bind-mounted host socket (DooD). The apt recipe
# mirrors the runner image's ``INSTALL_DOCKER_CLI`` layer.
_DOCKER_CLI_LAYER = """\
RUN apt-get update \\
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \\
    && install -m 0755 -d /etc/apt/keyrings \\
    && curl -fsSL https://download.docker.com/linux/debian/gpg \\
    -o /etc/apt/keyrings/docker.asc \\
    && echo "deb [arch=$(dpkg --print-architecture) \\
    signed-by=/etc/apt/keyrings/docker.asc] \\
    https://download.docker.com/linux/debian \\
    $(. /etc/os-release && echo $VERSION_CODENAME) stable" \\
    > /etc/apt/sources.list.d/docker.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \\
    && rm -rf /var/lib/apt/lists/*"""

_DOCKERFILE_TEMPLATE = """\
FROM {base_image}
{docker_cli_layer}
RUN pip install --no-cache-dir "harbor=={harbor_version}"
COPY {task_pack} {container_task_dir}
COPY tests /tests
WORKDIR /app
"""

# Self-contained (stdlib-only) reward extractor: read the one Harbor job result,
# take ``verifier_result.rewards.reward``, fall back to the mean of per-step
# rewards for a multi-step task, and write 0.0 when the run raised or produced no
# result. Harbor is never imported here — the verifier reads its JSON directly.
_TEST_SH = """\
#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
python3 - > /logs/verifier/reward.txt <<'PY'
import glob
import json

matches = sorted(glob.glob("/logs/harbor/trial/*__*/result.json"))
if not matches:
    print(0.0)
    raise SystemExit

with open(matches[0]) as handle:
    result = json.load(handle)

if result.get("exception_info") is not None:
    print(0.0)
    raise SystemExit


def reward_of(node):
    verifier_result = (node or {}).get("verifier_result") or {}
    rewards = verifier_result.get("rewards") or {}
    try:
        return float(rewards["reward"])
    except (KeyError, TypeError, ValueError):
        return None


top = reward_of(result)
if top is not None:
    print(top)
    raise SystemExit

steps = result.get("step_results") or []
values = [r for r in (reward_of(step) for step in steps) if r is not None]
print(sum(values) / len(values) if values else 0.0)
PY
"""


@dataclass(frozen=True)
class MaterialisedEnvironment:
    """A Harbor task's engine-ready environment materialised on disk."""

    compose_file: Path
    engine_compose_file: Path
    agent_service: str
    staging_dir: Path
    agent_image: str


def materialise_task_environment(
    pack_dir: Path,
    task_name: str,
    *,
    staging_root: Path,
    harbor_version: str,
    base_image: str,
    provider_env_keys: Sequence[str] = (),
    runner_image: str = "tolokaforge-runner:local",
    db_service_image: str = "tolokaforge-db-service:local",
    agent_service: str = "main",
) -> MaterialisedEnvironment:
    """Stage the TB2 task pack + build context and emit the two compose files."""
    digest = _digest(
        pack_dir,
        {
            "task_name": task_name,
            "harbor_version": harbor_version,
            "base_image": base_image,
            "provider_env_keys": ",".join(sorted(provider_env_keys)),
            "runner_image": runner_image,
            "db_service_image": db_service_image,
            "agent_service": agent_service,
        },
    )
    if digest.lower() in _FLOATING_IMAGE_TAGS:  # pragma: no cover - digests are hex
        digest = f"h{digest}"

    staging_dir = (staging_root / f"harbor-{_safe(task_name)}-{digest}").resolve()
    _write_staging(
        pack_dir,
        staging_dir,
        harbor_version=harbor_version,
        base_image=base_image,
    )

    agent_image = f"tolokaforge-harbor-{_safe(task_name)}:{digest}"
    task_doc = _task_compose_doc(agent_service, agent_image, provider_env_keys)
    compose_file = staging_dir / _TASK_COMPOSE_FILENAME
    compose_file.write_text(yaml.safe_dump(task_doc, sort_keys=False))

    engine_compose_file = _write_engine_stack(
        staging_root, _engine_compose_doc(runner_image, db_service_image)
    )

    return MaterialisedEnvironment(
        compose_file=compose_file.resolve(),
        engine_compose_file=engine_compose_file,
        agent_service=agent_service,
        staging_dir=staging_dir,
        agent_image=agent_image,
    )


def _write_staging(
    pack_dir: Path,
    staging_dir: Path,
    *,
    harbor_version: str,
    base_image: str,
) -> None:
    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == "__pycache__"]

    task_pack_dir = staging_dir / TASK_PACK_DIRNAME
    if task_pack_dir.exists():
        shutil.rmtree(task_pack_dir)
    shutil.copytree(pack_dir, task_pack_dir, ignore=_ignore)

    (staging_dir / "Dockerfile").write_text(
        _DOCKERFILE_TEMPLATE.format(
            base_image=base_image,
            docker_cli_layer=_DOCKER_CLI_LAYER,
            harbor_version=harbor_version,
            task_pack=TASK_PACK_DIRNAME,
            container_task_dir=CONTAINER_TASK_DIR,
        )
    )
    tests_dir = staging_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test.sh").write_text(_TEST_SH)
    (staging_dir / STAGING_LOGS_DIRNAME / "harbor").mkdir(parents=True, exist_ok=True)
    (staging_dir / STAGING_LOGS_DIRNAME / "verifier").mkdir(parents=True, exist_ok=True)


def _task_compose_doc(
    agent_service: str, agent_image: str, provider_env_keys: Sequence[str]
) -> dict[str, Any]:
    environment = {key: f"${{{provider_input(key)}}}" for key in sorted(provider_env_keys)}
    body: dict[str, Any] = {
        "image": agent_image,
        "build": {"context": ".", "dockerfile": "Dockerfile"},
        "container_name": f"{PROJECT_PREFIX}{_TRIAL_SLUG}_{agent_service}",
        "command": ["sleep", "infinity"],
        # DooD: the host Docker socket + the baked-in Docker CLI let ``harbor run``
        # shell ``docker compose`` to build Harbor's own task sandbox. The socket
        # is an absolute bind source; a task-stack compose the adapter synthesises
        # is not subject to the manifest's relative-bind-mount safety check, which
        # validates the engine mirror compose only.
        "volumes": [
            "./tests:/tests",
            f"./{STAGING_LOGS_DIRNAME}:{CONTAINER_LOGS_DIR}",
            f"{DOCKER_SOCKET_PATH}:{DOCKER_SOCKET_PATH}",
        ],
    }
    if environment:
        body["environment"] = environment
    return {"services": {agent_service: body}}


def _engine_compose_doc(runner_image: str, db_service_image: str) -> dict[str, Any]:
    return {
        "services": {
            _RUNNER_SERVICE: {
                "image": runner_image,
                "ports": ["50051"],
                "environment": {"DB_SERVICE_URL": "http://db-service:8000"},
                "healthcheck": {
                    "test": ["CMD", "bash", "-c", "echo > /dev/tcp/127.0.0.1/50051"],
                    "interval": "2s",
                    "timeout": "3s",
                    "retries": 30,
                    "start_period": "3s",
                },
                "depends_on": {_DB_SERVICE: {"condition": "service_healthy"}},
            },
            _DB_SERVICE: {
                "image": db_service_image,
                "ports": ["8000"],
                "healthcheck": {
                    "test": ["CMD-SHELL", "curl -fs http://localhost:8000/health || exit 1"],
                    "interval": "2s",
                    "timeout": "3s",
                    "retries": 30,
                    "start_period": "3s",
                },
            },
        }
    }


def _write_engine_stack(staging_root: Path, engine_doc: dict[str, Any]) -> Path:
    engine_bytes = yaml.safe_dump(engine_doc, sort_keys=False)
    digest = hashlib.sha256(engine_bytes.encode()).hexdigest()[:16]
    engine_dir = (staging_root / f"{_ENGINE_STAGING_DIRNAME}-{digest}").resolve()
    engine_dir.mkdir(parents=True, exist_ok=True)
    engine_compose_file = engine_dir / _ENGINE_COMPOSE_FILENAME
    if not engine_compose_file.exists():
        engine_compose_file.write_text(engine_bytes)
    return engine_compose_file


def provider_input(key: str) -> str:
    """Compose input variable name carrying the value for provider env ``key``."""
    return f"{PROJECT_PREFIX.upper()}PROVIDER_{key}"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).lower()


def _digest(pack_dir: Path, params: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(pack_dir.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        hasher.update(b"P|" + path.relative_to(pack_dir).as_posix().encode() + b"\n")
        if path.is_file():
            hasher.update(b"C|" + path.read_bytes() + b"\n")
    for key in sorted(params):
        hasher.update(f"{key}={params[key]}\n".encode())
    return hasher.hexdigest()[:16]
