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

Harbor's sandbox is a **second** DooD level: Harbor (running in the agent
container) shells ``docker compose`` against the host daemon to build and run its
sandbox, and bind-mounts its own job directory (the ``harbor run -o`` path, where
it writes ``result.json`` and the verifier output) INTO that sandbox. The host
daemon resolves that bind source on the HOST filesystem, so the job directory
must sit at an **identical absolute path on the host and inside the agent
container** — otherwise the host daemon cannot see the agent container's private
``/logs`` and refuses the mount ("Mounts denied" on Docker Desktop; a silently
empty auto-created dir on Linux). The job directory is therefore an *identity*
bind mount (``<staging>/harbor_jobs`` → the same path), and ``harbor run
--jobs-dir`` targets that absolute path. The tolokaforge verifier reward
(``/logs/verifier/reward.txt``, written by the generated ``tests/test.sh`` and
read by the runner over ``docker exec``) stays an in-container path — it rides no
nested mount, so it needs no identity treatment. In the orchestrator-image case
the staging root must itself be a host-visible path for the same reason (the DooD
caveat in ``docs/ORCHESTRATOR_IMAGE.md``).

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
from tolokaforge.core.grading.kinds.test_execution import UNGRADEABLE_SENTINEL
from tolokaforge.runner.models import _FLOATING_IMAGE_TAGS

PROJECT_PREFIX = "tf_harbor_"
CONTAINER_LOGS_DIR = "/logs"

# Per-trial Harbor job name. Harbor writes each run under
# ``<jobs-dir>/<job-name>/``; a name shared across trials of one task would
# collide when ``repeats>1`` / ``workers>1`` run that task concurrently. The
# name is therefore derived in-container from the per-trial slug the engine
# substitutes into every trial-scope stack, so the ``harbor run`` command and
# the verifier that reads the result agree on one unique directory without the
# adapter having to thread a per-trial value through two separate steps.
TRIAL_SLUG_ENV = "TOLOKAFORGE_TRIAL_SLUG"
_JOB_NAME_PREFIX = "tf"
# Shell expression (expanded inside the trial container by ``bash -c``) naming
# this trial's Harbor job: ``tf-<trial slug>``.
HARBOR_JOB_NAME_SHELL = f'{_JOB_NAME_PREFIX}-"${{{TRIAL_SLUG_ENV}}}"'
# Staging subdir identity-mounted (host path == container path) as Harbor's
# jobs directory, so Harbor's sandbox can bind-mount it via the host daemon.
HARBOR_JOBS_DIRNAME = "harbor_jobs"
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


def _render_test_sh(harbor_jobs_dir: Path, harbor_version: str) -> str:
    """The tolokaforge verifier: extract Harbor's reward and write it where the
    runner's ``test_execution`` grading reads it (``/logs/verifier/reward.txt``).

    Self-contained (stdlib-only): read the one Harbor job result under this
    trial's job directory, take ``verifier_result.rewards.reward`` (falling back
    to the mean of per-step rewards for a multi-step task), and write that real
    number. Harbor is never imported here — the verifier reads its JSON directly.

    ``harbor run`` exits 0 even when the agent never started, so a reward is only
    written when the result shows a **genuine** evaluation. When Harbor recorded
    an ``exception_info``, wrote no result at all, produced no ``verifier_result``,
    or the result does not match the pinned schema, the verifier writes
    :data:`~tolokaforge.core.grading.kinds.test_execution.UNGRADEABLE_SENTINEL`
    instead of a number and exits non-zero — so ``test_execution`` books the
    trial as a grading error rather than a legitimate ``0.0`` score.

    The job directory is resolved in-container from the per-trial slug the engine
    substitutes into the stack (``<harbor_jobs_dir>/tf-$TOLOKAFORGE_TRIAL_SLUG/``),
    read via an **exact** per-trial path rather than a "newest job" glob, so
    concurrent trials of one task cannot read each other's results. The pinned
    Harbor version is stamped into the verifier output so a schema drift is
    visible in the recorded trial rather than silently mis-parsed.
    """
    reward_path = f"{CONTAINER_LOGS_DIR}/verifier/reward.txt"
    return f"""\
#!/usr/bin/env bash
set -uo pipefail
python3 - <<'PY'
import glob
import json
import os
import sys

REWARD_PATH = {reward_path!r}
JOBS_DIR = {str(harbor_jobs_dir)!r}
HARBOR_VERSION = {harbor_version!r}
UNGRADEABLE = {UNGRADEABLE_SENTINEL!r}

print("harbor verifier: harbor==%s" % HARBOR_VERSION)


def _write(text):
    os.makedirs(os.path.dirname(REWARD_PATH), exist_ok=True)
    with open(REWARD_PATH, "w") as handle:
        handle.write("%s\\n" % text)


def ungradeable(reason):
    # Harbor can exit 0 without ever running the agent; a zero reward would read
    # as a real failing score. Write the sentinel so test_execution refuses the
    # trial (grading error), and exit non-zero with a diagnostic.
    print("harbor verifier: UNGRADEABLE: %s" % reason)
    sys.stderr.write("harbor verifier: UNGRADEABLE: %s\\n" % reason)
    _write(UNGRADEABLE)
    sys.exit(1)


slug = os.environ.get({TRIAL_SLUG_ENV!r}, "")
job_name = "{_JOB_NAME_PREFIX}-%s" % slug
pattern = os.path.join(JOBS_DIR, job_name, "*__*", "result.json")
matches = sorted(glob.glob(pattern))
if not matches:
    ungradeable(
        "harbor wrote no result.json at %r (the agent/sandbox never produced a "
        "graded trial)" % pattern
    )

try:
    with open(matches[0]) as handle:
        result = json.load(handle)
except (OSError, ValueError) as exc:
    ungradeable("harbor result.json at %r is unreadable: %s" % (matches[0], exc))

if not isinstance(result, dict):
    ungradeable(
        "harbor result.json is a %s, expected an object (schema drift for "
        "harbor==%s)" % (type(result).__name__, HARBOR_VERSION)
    )

if result.get("exception_info") is not None:
    ungradeable(
        "harbor recorded exception_info=%r (the trial raised before a real "
        "evaluation)" % (result.get("exception_info"),)
    )


def reward_of(node):
    if not isinstance(node, dict):
        return None
    verifier_result = node.get("verifier_result")
    if not isinstance(verifier_result, dict):
        return None
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict) or "reward" not in rewards:
        return None
    value = rewards["reward"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


top = reward_of(result)
if top is not None:
    _write(top)
    sys.exit(0)

steps = result.get("step_results") or []
values = [r for r in (reward_of(step) for step in steps) if r is not None]
if not values:
    ungradeable(
        "harbor result.json carries no verifier_result.rewards.reward and no "
        "per-step reward (no evaluation happened; harbor==%s)" % HARBOR_VERSION
    )
_write(sum(values) / len(values))
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
    harbor_jobs_dir: Path
    """Absolute host path identity-mounted as Harbor's ``-o`` job directory
    (host path == container path), so Harbor's sandbox can bind-mount it via the
    host daemon. The adapter passes this to ``harbor run -o``."""


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
    harbor_jobs_dir = staging_dir / HARBOR_JOBS_DIRNAME
    _write_staging(
        pack_dir,
        staging_dir,
        harbor_version=harbor_version,
        base_image=base_image,
        harbor_jobs_dir=harbor_jobs_dir,
    )

    agent_image = f"tolokaforge-harbor-{_safe(task_name)}:{digest}"
    task_doc = _task_compose_doc(agent_service, agent_image, provider_env_keys, harbor_jobs_dir)
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
        harbor_jobs_dir=harbor_jobs_dir,
    )


def _write_staging(
    pack_dir: Path,
    staging_dir: Path,
    *,
    harbor_version: str,
    base_image: str,
    harbor_jobs_dir: Path,
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
    (tests_dir / "test.sh").write_text(_render_test_sh(harbor_jobs_dir, harbor_version))
    # Identity-mounted job directory (host == container path); pre-created so the
    # bind mount carries the host's ownership rather than Docker's root-owned stub.
    harbor_jobs_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / STAGING_LOGS_DIRNAME / "verifier").mkdir(parents=True, exist_ok=True)


def _task_compose_doc(
    agent_service: str,
    agent_image: str,
    provider_env_keys: Sequence[str],
    harbor_jobs_dir: Path,
) -> dict[str, Any]:
    environment = {key: f"${{{provider_input(key)}}}" for key in sorted(provider_env_keys)}
    # The engine substitutes a unique per-trial slug into every trial-scope
    # stack; carrying it into the container's env lets the ``harbor run`` command
    # and the verifier both derive this trial's unique Harbor job name from it.
    environment[TRIAL_SLUG_ENV] = f"${{{TRIAL_SLUG_ENV}}}"
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
        #
        # The job directory is an IDENTITY mount (same absolute path on host and
        # in the container) so Harbor's sandbox — a second DooD level built on the
        # host daemon — can bind-mount it; a non-identity path is invisible to the
        # host daemon and the mount is refused. See the module docstring.
        "volumes": [
            "./tests:/tests",
            f"./{STAGING_LOGS_DIRNAME}:{CONTAINER_LOGS_DIR}",
            f"{harbor_jobs_dir}:{harbor_jobs_dir}",
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
