"""Compose synthesis for the native adapter's coding-harness mode.

The native adapter runs a per-trial compose stack when it opts into
``models.agent.harness``: the trial container is the CLI's workspace, the
tolokaforge runner sidecar carries the gRPC surface the conductor drives
through, and the db-service sidecar backs the runner's state RPCs.

The pack ships a task-local ``docker-compose.yaml`` declaring only the main
service; this module materialises a per-task staging directory whose
synthesised compose file adds:

- a build-only ``main_base`` service pinning the pack's own image build so
  the harness Dockerfile can ``FROM`` its tag;
- a harness image layer on ``main`` via
  :meth:`~tolokaforge_coding_harnesses.adapter_support.CodingHarnessAdapterMixin.write_install_script_layer`;
- the tolokaforge ``runner`` and ``db-service`` sidecars the trial's gRPC
  path needs.

The layered image is what :meth:`~tolokaforge.adapters.native.NativeAdapter.docker_stack_requirements`
declares in :class:`~tolokaforge.adapters.base.ComposeImageBuild` entries so
the orchestrator pre-builds both stages before any trial provisions.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tolokaforge_coding_harnesses import HarnessSpec

PROJECT_PREFIX = "tfnative_"
"""Compose project prefix baked into the synthesised container names.

Read by the runner-side
:class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper` to
resolve the per-trial container the CLI's bash tool execs into. The
synthesised compose file interpolates
``container_name: tfnative_${TOLOKAFORGE_TRIAL_SLUG}_<service>`` so the two
sides agree on the name docker compose actually starts."""

BASE_SERVICE_SUFFIX = "_base"
"""Suffix on the build-only service pinning the pack's own image build."""

RUNNER_SERVICE = "runner"
DB_SERVICE = "db-service"

_DEFAULT_RUNNER_IMAGE = "tolokaforge-runner:local"
_DEFAULT_DB_SERVICE_IMAGE = "tolokaforge-db-service:local"

_HARNESS_BUILD_PROFILE = "_harness_build_only"
"""Compose profile that keeps ``main_base`` out of ``docker compose up``."""

_TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER = "${TOLOKAFORGE_TRIAL_SLUG}"


@dataclass(frozen=True)
class MaterialisedHarnessEnvironment:
    """One synthesised staging directory for a task under harness mode.

    Attributes:
        compose_file: Absolute path to the synthesised compose file.
        agent_service: Compose service the CLI's bash tool execs into.
        base_build_service: Companion build-only service the harness layer
            ``FROM``s. The orchestrator builds it before the layered
            ``agent_service`` (see
            :meth:`~tolokaforge.adapters.native.NativeAdapter.docker_stack_requirements`).
        staging_dir: Root of the per-task build context (also the
            ``build.context`` the layered dockerfile is invoked with).
    """

    compose_file: Path
    agent_service: str
    base_build_service: str
    staging_dir: Path


def resolve_agent_service(compose_file: Path, task_id: str) -> str:
    """Return the compose service the harness bash tool execs into.

    Picks the service named ``main`` when present; otherwise the sole
    service. Refuses ambiguity so the wire target is unambiguous.

    Raises:
        FileNotFoundError: *compose_file* does not exist.
        ValueError: The compose file declares no services, or several
            services none of which is named ``main``.
    """
    if not compose_file.exists():
        raise FileNotFoundError(
            f"native harness adapter: task {task_id!r} declares "
            f"environment_manifest.stack.compose_file={compose_file!r}, "
            "but that file does not exist"
        )
    doc = yaml.safe_load(compose_file.read_text()) or {}
    services = doc.get("services") or {}
    if not services:
        raise ValueError(
            f"native harness adapter: task {task_id!r} compose file "
            f"{compose_file!r} declares no services"
        )
    if "main" in services:
        return "main"
    if len(services) == 1:
        return next(iter(services))
    raise ValueError(
        f"native harness adapter: task {task_id!r} compose file "
        f"{compose_file!r} declares {sorted(services)!r} but no service is "
        "named 'main'; rename one to 'main' or leave a single service in the "
        "compose file"
    )


def materialise_harness_environment(
    *,
    task_id: str,
    task_dir: Path,
    compose_file: Path,
    harness_spec: HarnessSpec,
    agent_harness: str,
    layer_writer: Any,
    staging_root: Path | None = None,
    runner_image: str = _DEFAULT_RUNNER_IMAGE,
    db_service_image: str = _DEFAULT_DB_SERVICE_IMAGE,
) -> MaterialisedHarnessEnvironment:
    """Stage this task's harness-mode compose stack under *staging_root*.

    Copies the pack directory into a per-task staging directory keyed by a
    content digest, writes the harness Dockerfile via *layer_writer* (a
    :meth:`~tolokaforge_coding_harnesses.adapter_support.CodingHarnessAdapterMixin.write_install_script_layer`-shaped
    callable), and materialises a synthesised compose file with the harness
    layer + runner + db-service sidecars.

    Raises:
        ValueError: The pack compose file has no ``main`` service (see
            :func:`resolve_agent_service`) or the target service's ``build``
            entry does not name a build context.
    """
    agent_service = resolve_agent_service(compose_file, task_id)
    root = staging_root or Path(tempfile.gettempdir()) / "tolokaforge-native-harness"
    digest = _content_digest(
        task_dir,
        agent_harness=agent_harness,
        harness_version=harness_spec.version,
        harness_install_source=harness_spec.install_source,
    )
    staging_dir = (root / f"{task_id}-{digest}").resolve()
    _copy_task_dir(task_dir, staging_dir)

    base_image = _base_image_tag(task_id)
    layered_image = _layered_image_tag(task_id, agent_harness, harness_spec.version)
    base_service = f"{agent_service}{BASE_SERVICE_SUFFIX}"

    layer_writer(
        context_dir=staging_dir,
        base_image=base_image,
        spec=harness_spec,
        middleware_proxy=harness_spec.request_middleware is not None,
    )

    synthesised = _synthesise_compose(
        original_compose=yaml.safe_load(compose_file.read_text()) or {},
        agent_service=agent_service,
        base_service=base_service,
        base_image=base_image,
        layered_image=layered_image,
        harness_spec=harness_spec,
        runner_image=runner_image,
        db_service_image=db_service_image,
    )
    staged_compose = staging_dir / "docker-compose.yaml"
    staged_compose.write_text(yaml.safe_dump(synthesised, sort_keys=False))
    return MaterialisedHarnessEnvironment(
        compose_file=staged_compose.resolve(),
        agent_service=agent_service,
        base_build_service=base_service,
        staging_dir=staging_dir,
    )


def _copy_task_dir(task_dir: Path, staging_dir: Path) -> None:
    """Copy *task_dir* into *staging_dir* excluding the oracle and caches.

    ``solution/`` is dropped so the CLI cannot exec it, ``__pycache__``
    directories are dropped so a stale cache does not follow the copy.
    """

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in ("solution", "__pycache__")]

    shutil.copytree(task_dir, staging_dir, ignore=_ignore, dirs_exist_ok=True)


def _synthesise_compose(
    *,
    original_compose: dict[str, Any],
    agent_service: str,
    base_service: str,
    base_image: str,
    layered_image: str,
    harness_spec: HarnessSpec,
    runner_image: str,
    db_service_image: str,
) -> dict[str, Any]:
    """Return the synthesised compose doc — pack layout + harness + sidecars."""
    doc = deepcopy(original_compose)
    services: dict[str, Any] = doc.setdefault("services", {})

    agent_body: dict[str, Any] = services.get(agent_service, {})
    task_build = agent_body.get("build")
    if task_build is None:
        raise ValueError(
            f"native harness adapter: compose service {agent_service!r} must "
            "declare a `build:` entry the harness layer FROMs; got no build"
        )

    agent_body["image"] = layered_image
    agent_body["build"] = {"context": ".", "dockerfile": "harness.Dockerfile"}
    agent_body["container_name"] = (
        f"{PROJECT_PREFIX}{_TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER}_{agent_service}"
    )
    # `depends_on: runner` on the agent service would loop — the runner
    # depends on db-service, and neither depends on the CLI target.
    agent_body_env = _set_env(agent_body.get("environment"), "TEST_DIR", "/tests")
    for key, value in sorted(harness_spec.container_env.items()):
        agent_body_env = _set_env(agent_body_env, key, value)
    agent_body["environment"] = agent_body_env
    services[agent_service] = agent_body

    services[base_service] = {
        "image": base_image,
        "build": deepcopy(task_build),
        "profiles": [_HARNESS_BUILD_PROFILE],
    }

    services[RUNNER_SERVICE] = _runner_service_body(runner_image)
    services[DB_SERVICE] = _db_service_body(db_service_image)
    return doc


def _runner_service_body(runner_image: str) -> dict[str, Any]:
    return {
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
        "depends_on": {
            DB_SERVICE: {"condition": "service_healthy"},
        },
    }


def _db_service_body(db_service_image: str) -> dict[str, Any]:
    return {
        "image": db_service_image,
        "ports": ["8000"],
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -fs http://localhost:8000/health || exit 1"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
            "start_period": "3s",
        },
    }


def _set_env(existing: Any, key: str, value: str) -> Any:
    """Set ``key=value`` on a compose service's ``environment:`` value.

    Preserves the shape the pack declared (list of ``KEY=value`` strings or
    mapping); replaces any prior entry for the same key rather than
    duplicating it.
    """
    if isinstance(existing, dict):
        return {**existing, key: value}
    if isinstance(existing, list):
        filtered = [
            entry
            for entry in existing
            if not (isinstance(entry, str) and entry.split("=", 1)[0] == key)
        ]
        filtered.append(f"{key}={value}")
        return filtered
    return {key: value}


def _content_digest(
    task_dir: Path,
    *,
    agent_harness: str,
    harness_version: str,
    harness_install_source: str,
) -> str:
    """Content hash of *task_dir* + the harness parameters the layer bakes in."""
    hasher = hashlib.sha256()
    for path in sorted(task_dir.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(task_dir).as_posix()
        hasher.update(b"P|" + rel.encode() + b"\n")
        if path.is_file():
            hasher.update(b"C|")
            hasher.update(path.read_bytes())
            hasher.update(b"\n")
    hasher.update(b"|harness|\n")
    hasher.update(f"agent_harness={agent_harness}\n".encode())
    hasher.update(f"harness_version={harness_version}\n".encode())
    hasher.update(f"harness_install_source={harness_install_source}\n".encode())
    return hasher.hexdigest()[:16]


def _base_image_tag(task_id: str) -> str:
    return f"tolokaforge-native-{task_id}-base:local"


def _layered_image_tag(task_id: str, agent_harness: str, harness_version: str) -> str:
    return f"tolokaforge-native-{task_id}-{agent_harness}-{harness_version}:local"
