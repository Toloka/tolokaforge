"""Synthesise an engine-provisionable compose file from a terminal-bench task.

Terminal-bench tasks author their ``docker-compose.yaml`` for terminal-bench's
own provisioner, which injects a ``T_BENCH_*`` variable set (plus ``CPUS`` /
``MEMORY``) at up-time. The tolokaforge engine never sets those, so
provisioning would fail at compose parse or leave the container name
unresolved. This module resolves the adapter-owned variable set at synthesis
time, pins the agent-service image, replaces the log bind-mounts with
relative mounts against the staging directory, and injects the engine's
``runner`` + ``db-service`` alongside the task's own services — so the
emitted compose file is a self-contained trial substrate the engine's
per-trial runtime can bring up unchanged.

Under harness mode the agent image is split in two: the task's own build
becomes a build-only ``-base`` service, and the agent service builds a thin
layer on top that installs the requested coding-harness CLI. Both images are
declared to the orchestrator's pre-build seam, base first.

The module runs no subprocess. Both adapter surfaces that call it
(``get_task`` and ``to_task_description``) stay daemon-free, which the
canonical adapter lane and ``--dry-run`` both require. The agent image is
built declaratively via :class:`~tolokaforge.adapters.base.ComposeImageBuild`
in :meth:`docker_stack_requirements`; nothing in this module shells out.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Shared with EnvironmentManifest's `_check_pinned_images` so a floating tag
# fails in the adapter with the adapter's own message rather than deep inside
# a Pydantic validator on the synthesised compose file.
from tolokaforge.runner.models import _FLOATING_IMAGE_TAGS
from tolokaforge_adapter_terminal_bench.harness import (
    INSTALL_SCRIPT,
    NO_OP_HARNESS,
    provider_env_input,
    validate_harness,
)
from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

AGENT_SERVICE_DEFAULT = "main"
PROJECT_PREFIX = "tbench_"

_SYNTHESISED_COMPOSE_FILENAME = "docker-compose.tolokaforge.yaml"
_INJECTED_SERVICE_NAMES = ("runner", "db-service")
_TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER = "${TOLOKAFORGE_TRIAL_SLUG}"

_HARNESS_STAGING_DIR = "_harness"
_HARNESS_DOCKERFILE_NAME = "harness.Dockerfile"
_HARNESS_BASE_SERVICE_SUFFIX = "-base"
_HARNESS_BUILD_PROFILE = "tolokaforge-build"
_HARNESS_INSTALL_PATH = "/opt/tolokaforge/install-harness.sh"

# Matches ``${VAR}`` and ``${VAR:-default}``. Docker compose's own
# variable-substitution surface is a superset (``${VAR-default}``, ``${VAR:?err}``,
# ``${VAR?err}``); the adapter-owned set only ever appears in the two forms
# covered here, so extending the pattern would add unreachable branches.
_SUBSTITUTION_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class MaterialisedEnvironment:
    """A task's engine-ready environment materialised on disk."""

    compose_file: Path
    """Absolute path to the synthesised ``docker-compose.tolokaforge.yaml``."""

    agent_service: str
    """The compose service the bash tool execs into."""

    staging_dir: Path
    """Absolute path to the staging directory the compose file lives in."""

    base_build_service: str | None = None
    """Compose service that builds the un-layered task image, when the
    environment is harness-layered and the base is built rather than pulled.
    The orchestrator must build this service before the agent service, whose
    Dockerfile is ``FROM`` the base image. ``None`` when there is a single
    image to build (or pull)."""


def materialise_task_environment(
    meta: TerminalBenchTask,
    *,
    staging_root: Path,
    image_registry: str | None = None,
    image_tag: str = "local",
    agent_harness: str = NO_OP_HARNESS,
    provider_env_keys: Sequence[str] = (),
    runner_image: str = "tolokaforge-runner:local",
    db_service_image: str = "tolokaforge-db-service:local",
) -> MaterialisedEnvironment:
    """Copy the task pack into a staging directory and emit a synthesised compose file.

    The staging directory is ``staging_root / f"{meta.task_id}-{digest}"``,
    where ``digest`` is a content hash over the task directory and the
    synthesis parameters. Re-running with identical inputs is idempotent —
    the digest resolves to the same path and the copy is refreshed in place.

    Args:
        meta: Parsed terminal-bench task metadata.
        staging_root: Root under which each task materialises its own
            digest-named subdirectory.
        image_registry: When set, the agent service's ``image:`` is pinned to
            ``{image_registry}/{task_id}:{image_tag}`` and its ``build:``
            section is dropped so the image is pulled instead of built.
            ``None`` pins a local tag (``tbench-{task_id}:{image_tag}``) and
            keeps the task's ``build:`` context so the orchestrator can
            build it declaratively.
        image_tag: Tag applied to the agent-service image. Must not be a
            floating tag (``latest``, ``main``, ``master``, ...); the
            manifest's floating-tag rule is applied here.
        agent_harness: Coding-harness CLI to layer onto the task image.
            ``terminus-2`` leaves the image and the compose file untouched.
            Any other accepted harness splits the agent image in two: the
            task's own build becomes the ``-base`` image, and the agent
            service builds a thin layer on top of it that installs the CLI.
            The layered image carries the harness in its tag, so switching
            harnesses can never reuse a stale cached image.
        provider_env_keys: Environment-variable names the agent service
            receives. Each is bound to an adapter-namespaced compose input
            (``KEY=${TBENCH_PROVIDER_KEY}``) that the per-trial ``.env``
            supplies at up-time. Names only — the values never enter the
            compose file, the staging digest, or the image.
        runner_image: Pinned image for the injected ``runner`` service.
        db_service_image: Pinned image for the injected ``db-service``.

    Raises:
        ValueError: If ``image_tag`` is a floating tag; if ``agent_harness``
            is not an accepted harness; if the task's compose file is not a
            YAML mapping with a non-empty ``services:`` block; if the task
            declares a service named ``runner`` or ``db-service`` (collision
            with the injected engine services) or one colliding with the
            harness base service; if the task's ``services:`` mapping
            declares more than one service and none is named ``main``.
    """
    if image_tag.lower() in _FLOATING_IMAGE_TAGS:
        raise ValueError(
            f"terminal-bench adapter: image_tag {image_tag!r} is a floating tag; "
            "pin to an immutable tag (e.g. 'local' for local builds, or a digest)."
        )
    validate_harness(agent_harness)

    original = _load_compose(meta.compose_file)
    task_services = original.get("services")
    if not isinstance(task_services, dict) or not task_services:
        raise ValueError(
            f"terminal-bench task {meta.task_id!r} compose file "
            f"{meta.compose_file} must declare a non-empty `services:` mapping."
        )
    agent_service = _resolve_agent_service(meta.task_id, task_services)
    base_service = f"{agent_service}{_HARNESS_BASE_SERVICE_SUFFIX}"
    _check_no_reserved_service_collisions(meta.task_id, task_services, base_service)

    digest = _compute_digest(
        meta.task_dir,
        {
            "image_registry": image_registry or "",
            "image_tag": image_tag,
            "agent_harness": agent_harness,
            "provider_env_keys": ",".join(sorted(provider_env_keys)),
            "runner_image": runner_image,
            "db_service_image": db_service_image,
            "cpus": str(meta.cpus),
            "memory_mb": str(meta.memory_mb),
            "agent_service": agent_service,
        },
    )
    staging_dir = (staging_root / f"{meta.task_id}-{digest}").resolve()
    _write_staging(meta.task_dir, staging_dir)

    synthesised, base_build_service = _build_synthesised_compose(
        original=original,
        meta=meta,
        agent_service=agent_service,
        base_service=base_service,
        image_registry=image_registry,
        image_tag=image_tag,
        agent_harness=agent_harness,
        provider_env_keys=provider_env_keys,
        runner_image=runner_image,
        db_service_image=db_service_image,
    )
    if agent_harness != NO_OP_HARNESS:
        _write_harness_build_context(
            staging_dir,
            base_image=_agent_image(meta.task_id, image_registry, image_tag),
            agent_harness=agent_harness,
        )
    compose_file = staging_dir / _SYNTHESISED_COMPOSE_FILENAME
    compose_file.write_text(yaml.safe_dump(synthesised, sort_keys=False))

    return MaterialisedEnvironment(
        compose_file=compose_file.resolve(),
        agent_service=agent_service,
        staging_dir=staging_dir,
        base_build_service=base_build_service,
    )


def _load_compose(path: Path) -> dict[str, Any]:
    with path.open() as f:
        content = yaml.safe_load(f)
    if not isinstance(content, dict):
        raise ValueError(
            f"terminal-bench compose file {path} must be a YAML mapping; got "
            f"{type(content).__name__}."
        )
    return content


def _resolve_agent_service(task_id: str, services: dict[str, Any]) -> str:
    if AGENT_SERVICE_DEFAULT in services:
        return AGENT_SERVICE_DEFAULT
    if len(services) == 1:
        return next(iter(services))
    raise ValueError(
        f"terminal-bench task {task_id!r}: cannot resolve the agent service — "
        f"the compose file declares {sorted(services)!r} but no service is named "
        f"{AGENT_SERVICE_DEFAULT!r}. Rename one of the services to "
        f"{AGENT_SERVICE_DEFAULT!r} or leave a single service in the compose file."
    )


def _check_no_reserved_service_collisions(
    task_id: str, services: dict[str, Any], base_service: str
) -> None:
    reserved_names = (*_INJECTED_SERVICE_NAMES, base_service)
    for reserved in reserved_names:
        if reserved in services:
            raise ValueError(
                f"terminal-bench task {task_id!r} compose file declares a service "
                f"named {reserved!r}; the adapter injects services named "
                f"{list(reserved_names)!r}, which would silently replace "
                f"the task's own {reserved!r}. Rename it."
            )


def _compute_digest(task_dir: Path, params: dict[str, str]) -> str:
    """Content-hash the task directory + synthesis parameters.

    Sorted walk over the task directory (skipping ``__pycache__``) plus a
    stable serialisation of ``params`` — identical inputs yield the same
    digest so the staging path is stable across runs.
    """
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
    hasher.update(b"|params|\n")
    for key in sorted(params):
        hasher.update(f"{key}={params[key]}\n".encode())
    return hasher.hexdigest()[:16]


def _write_staging(task_dir: Path, staging_dir: Path) -> None:
    """Copy the task directory into ``staging_dir`` and set up the trial layout.

    Copies exclude ``__pycache__``. When the task ships ``run-tests.sh`` at
    its root and no ``tests/test.sh``, the root script is copied into place
    at ``tests/test.sh``. Empty ``_logs/verifier`` and ``_logs/agent``
    directories are created so ``copy_compose_context``'s per-trial copy
    preserves them for the agent-service log mounts.
    """

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == "__pycache__"]

    shutil.copytree(task_dir, staging_dir, ignore=_ignore, dirs_exist_ok=True)
    tests_dir = staging_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    if not (tests_dir / "test.sh").exists():
        root_script = staging_dir / "run-tests.sh"
        if root_script.exists():
            shutil.copy2(root_script, tests_dir / "test.sh")
    (staging_dir / "_logs" / "verifier").mkdir(parents=True, exist_ok=True)
    (staging_dir / "_logs" / "agent").mkdir(parents=True, exist_ok=True)


def _write_harness_build_context(staging_dir: Path, *, base_image: str, agent_harness: str) -> None:
    """Materialise the harness image layer's build context in the staging dir.

    The layer is one ``COPY`` of the install script plus one ``RUN`` of it
    against *base_image*. Both live under ``_harness/`` so a task pack that
    ships its own ``install-harness.sh`` or ``harness.Dockerfile`` at its root
    cannot collide with them.
    """
    harness_dir = staging_dir / _HARNESS_STAGING_DIR
    harness_dir.mkdir(exist_ok=True)
    shutil.copy2(INSTALL_SCRIPT, harness_dir / INSTALL_SCRIPT.name)
    (harness_dir / _HARNESS_DOCKERFILE_NAME).write_text(
        f"FROM {base_image}\n"
        f"COPY {_HARNESS_STAGING_DIR}/{INSTALL_SCRIPT.name} {_HARNESS_INSTALL_PATH}\n"
        f"RUN sh {_HARNESS_INSTALL_PATH} {agent_harness}\n"
    )


def _agent_image(task_id: str, image_registry: str | None, image_tag: str) -> str:
    if image_registry:
        return f"{image_registry}/{task_id}:{image_tag}"
    return f"tbench-{task_id}:{image_tag}"


def _build_synthesised_compose(
    *,
    original: dict[str, Any],
    meta: TerminalBenchTask,
    agent_service: str,
    base_service: str,
    image_registry: str | None,
    image_tag: str,
    agent_harness: str,
    provider_env_keys: Sequence[str],
    runner_image: str,
    db_service_image: str,
) -> tuple[dict[str, Any], str | None]:
    """Synthesised compose document, plus the base-build service name.

    The second element is the service the orchestrator must build *before*
    the agent service — non-``None`` only when a harness layer sits on top of
    a locally-built task image.
    """
    base_image = _agent_image(meta.task_id, image_registry, image_tag)
    agent_image = base_image if agent_harness == NO_OP_HARNESS else f"{base_image}-{agent_harness}"
    agent_container_name = f"{PROJECT_PREFIX}{_TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER}_{agent_service}"

    resolved_vars = {
        "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": agent_image,
        "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": agent_container_name,
        "T_BENCH_CONTAINER_LOGS_PATH": "/logs",
        "T_BENCH_TASK_LOGS_PATH": "./_logs",
        "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/logs/agent",
        "T_BENCH_TASK_AGENT_LOGS_PATH": "./_logs/agent",
        "T_BENCH_TEST_DIR": "/tests",
        "CPUS": str(meta.cpus),
        "MEMORY": f"{meta.memory_mb}M",
    }
    doc = _substitute_tree(deepcopy(original), resolved_vars)

    services: dict[str, Any] = doc["services"]
    agent_body: dict[str, Any] = services[agent_service]
    task_build = agent_body.get("build")
    agent_body["image"] = agent_image
    if image_registry:
        agent_body.pop("build", None)
    agent_body["container_name"] = agent_container_name
    agent_body["volumes"] = ["./tests:/tests", "./_logs:/logs"]
    agent_body["environment"] = _set_env_key(agent_body.get("environment"), "TEST_DIR", "/tests")
    for key in sorted(provider_env_keys):
        agent_body["environment"] = _set_env_key(
            agent_body["environment"], key, f"${{{provider_env_input(key)}}}"
        )

    base_build_service: str | None = None
    if agent_harness != NO_OP_HARNESS:
        agent_body["build"] = {
            "context": ".",
            "dockerfile": f"{_HARNESS_STAGING_DIR}/{_HARNESS_DOCKERFILE_NAME}",
        }
        if task_build is not None and not image_registry:
            services[base_service] = _harness_base_service_body(base_image, task_build)
            base_build_service = base_service

    services["runner"] = _runner_service_body(runner_image, agent_service)
    services["db-service"] = _db_service_body(db_service_image)
    return doc, base_build_service


def _substitute_tree(node: Any, values: dict[str, str]) -> Any:
    """Recursively resolve ``${VAR}`` / ``${VAR:-default}`` for adapter-owned keys."""
    if isinstance(node, dict):
        return {k: _substitute_tree(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_tree(x, values) for x in node]
    if isinstance(node, str):
        return _substitute_str(node, values)
    return node


def _substitute_str(s: str, values: dict[str, str]) -> str:
    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        default = m.group(2)
        if var in values:
            return values[var]
        if default is not None:
            return default
        return m.group(0)

    return _SUBSTITUTION_PATTERN.sub(_replace, s)


def _set_env_key(existing: Any, key: str, value: str) -> Any:
    """Set ``key=value`` on a compose service's ``environment:`` value,
    preserving its declared shape (list of ``KEY=value`` strings or mapping).
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


def _harness_base_service_body(base_image: str, task_build: Any) -> dict[str, Any]:
    """Build-only service carrying the task's own image build.

    The harness layer is ``FROM`` this service's image, so the two builds must
    be separately addressable — ``docker compose build`` takes a service name,
    not an image tag. The compose profile keeps it out of ``docker compose up``:
    nothing runs in this container, it exists so the base image has a name the
    orchestrator can build.
    """
    return {
        "image": base_image,
        "build": deepcopy(task_build),
        "profiles": [_HARNESS_BUILD_PROFILE],
    }


def _runner_service_body(runner_image: str, agent_service: str) -> dict[str, Any]:
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
            agent_service: {"condition": "service_started"},
            "db-service": {"condition": "service_healthy"},
        },
    }


def _db_service_body(db_service_image: str) -> dict[str, Any]:
    # The engine's ``tolokaforge-db-service`` image ships ``curl`` and no
    # ``wget``; matches the image's own ``HEALTHCHECK`` while tightening the
    # timings for per-trial provisioning.
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
