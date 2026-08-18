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
import os.path
import re
import shlex
import shutil
import warnings
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

# Shared with EnvironmentManifest's `_check_pinned_images` so a floating tag
# fails in the adapter with the adapter's own message rather than deep inside
# a Pydantic validator on the synthesised compose file.
from tolokaforge.runner.models import _FLOATING_IMAGE_TAGS
from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask
from tolokaforge_coding_harnesses import (
    DEFAULT_PATH_RESOLVER,
    ENGINE_LOOP,
    HARNESSES,
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    PATH_CONSTRUCT_PATTERN,
    HarnessSpec,
    PathResolver,
    SkillDelivery,
    SkillsBundle,
    provider_env_input,
    validate_harness,
)

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
    agent_harness: str = ENGINE_LOOP,
    harness_registry: Mapping[str, HarnessSpec] = HARNESSES,
    provider_env_keys: Sequence[str] = (),
    runner_image: str = "tolokaforge-runner:local",
    db_service_image: str = "tolokaforge-db-service:local",
    path_resolver: PathResolver | None = None,
    skill_delivery: SkillDelivery | None = None,
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
            ``engine-loop`` leaves the image and the compose file untouched.
            Any other accepted harness splits the agent image in two: the
            task's own build becomes the ``-base`` image, and the agent
            service builds a thin layer on top of it that installs the CLI.
            The layered image carries the harness in its tag, so switching
            harnesses (or bumping a pinned CLI version) can never reuse a
            stale cached image.
        harness_registry: Specs ``agent_harness`` resolves against. Defaults
            to the shipped registry; the adapter passes its own when an
            operator overlay replaced or added an entry.
        provider_env_keys: Environment-variable names the agent service
            receives. Each is bound to an adapter-namespaced compose input
            (``KEY=${TBENCH_PROVIDER_KEY}``) that the per-trial ``.env``
            supplies at up-time. Names only — the values never enter the
            compose file, the staging digest, or the image.
        runner_image: Pinned image for the injected ``runner`` service.
        db_service_image: Pinned image for the injected ``db-service``.
        path_resolver: Answers the runtime's filesystem conventions for the
            harness's ``skills_dir_target``. Defaults to
            :data:`~tolokaforge_coding_harnesses.DEFAULT_PATH_RESOLVER`.
        skill_delivery: Puts the task's skills bundle where the CLI reads it.
            Defaults to :data:`DEFAULT_SKILL_DELIVERY`, an image-layer ``COPY``.

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
    validate_harness(agent_harness, harness_registry)
    harness_spec = harness_registry.get(agent_harness)

    original = _load_compose(meta.compose_file)
    task_services = original.get("services")
    if not isinstance(task_services, dict) or not task_services:
        raise ValueError(
            f"terminal-bench task {meta.task_id!r} compose file "
            f"{meta.compose_file} must declare a non-empty `services:` mapping."
        )
    agent_service = _resolve_agent_service(meta.task_id, task_services)
    base_service = f"{agent_service}{_HARNESS_BASE_SERVICE_SUFFIX}"
    # The base service exists only under harness mode, so only harness mode can
    # collide with a task that happens to declare that name.
    _check_no_reserved_service_collisions(
        meta.task_id,
        task_services,
        base_service if agent_harness != ENGINE_LOOP else None,
    )

    resolver = DEFAULT_PATH_RESOLVER if path_resolver is None else path_resolver
    delivery = DEFAULT_SKILL_DELIVERY if skill_delivery is None else skill_delivery

    digest = _compute_digest(
        meta.task_dir,
        {
            "image_registry": image_registry or "",
            "image_tag": image_tag,
            "agent_harness": agent_harness,
            # The spec's whole content, because the staging dir carries the
            # generated harness Dockerfile: two adapters differing only by an
            # overlaid spec would otherwise share a staging dir and one would
            # overwrite the other's build context.
            "harness_spec": harness_spec.model_dump_json() if harness_spec else "",
            # Both seams move those same generated bytes: the resolver decides
            # the skills destination, the delivery whether a COPY is written at
            # all. The delivery contributes its type, not its value — a default
            # `repr` carries an address and would move the staging path per run.
            "skills_target": (
                resolver.resolve(harness_spec.skills_dir_target)
                if harness_spec and harness_spec.skills_dir_target
                else ""
            ),
            "skill_delivery": f"{type(delivery).__module__}.{type(delivery).__qualname__}",
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
        harness_spec=harness_spec,
        provider_env_keys=provider_env_keys,
        runner_image=runner_image,
        db_service_image=db_service_image,
    )
    if harness_spec is not None:
        skills_dir = installable_skills_dir(meta, harness_spec)
        if meta.harness_skills_dir is not None and skills_dir is None:
            # Dropped rather than refused, so one task still runs under every
            # harness — but never silently: a trial whose agent had no skills
            # must not read back as one that did.
            warnings.warn(
                f"terminal-bench task {meta.task_id!r} declares harness_skills_dir "
                f"{meta.harness_skills_dir!r}, but the selected harness declares no "
                "skills_dir_target; the bundle is not installed and the agent runs "
                "without it.",
                stacklevel=2,
            )
        _write_harness_build_context(
            staging_dir,
            base_image=_agent_image(meta.task_id, image_registry, image_tag),
            spec=harness_spec,
        )
        if skills_dir is not None:
            delivery.deliver(
                SkillsBundle(
                    task_dir=meta.task_dir,
                    source_rel=skills_dir,
                    target=resolver.resolve(harness_spec.skills_dir_target),
                    staging_dir=staging_dir,
                )
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
    task_id: str, services: dict[str, Any], base_service: str | None
) -> None:
    reserved_names = _INJECTED_SERVICE_NAMES + ((base_service,) if base_service else ())
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


def installable_skills_dir(meta: TerminalBenchTask, spec: HarnessSpec) -> str | None:
    """The task's skills bundle when *spec*'s harness has somewhere to put it.

    The single answer to "was a bundle handed to the run's ``SkillDelivery``":
    delivery is called with what this returns, and the artifact records a
    bundle hash exactly when it returns one. Split answers would let a trial
    claim a bundle nobody was asked to place. That the bundle then *arrives* is
    the delivery's own contract — one that cannot place it raises rather than
    returning quietly.
    """
    if spec.skills_dir_target is None:
        return None
    return meta.harness_skills_dir


def skills_bundle_digest(task_dir: Path, skills_dir: str) -> str:
    """Content hash of the skills bundle at ``task_dir / skills_dir``.

    Each file contributes its task-relative path and the sha256 of its bytes;
    the pairs are hashed in sorted path order, so the value is independent of
    filesystem walk order and moves when a file is added, removed, renamed, or
    edited. A rename alone has to move it: a skill's path is how the CLI
    discovers it, so two bundles differing only in layout are two different
    things to be told apart on the artifact.
    """
    root = task_dir / skills_dir
    hasher = hashlib.sha256()
    files = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    for rel, path in files:
        hasher.update(f"{rel}\n{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return hasher.hexdigest()


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


def _write_harness_build_context(staging_dir: Path, *, base_image: str, spec: HarnessSpec) -> None:
    """Materialise the harness image layer's build context in the staging dir.

    The layer is one ``COPY`` of the install script plus one ``RUN`` of it
    against *base_image*, installing the version the spec pins. The install
    script lives under ``_harness/`` so a task pack shipping its own
    ``install-harness.sh`` or ``harness.Dockerfile`` at its root cannot collide
    with it, and a ``.dockerignore`` keeps the rest of the staging tree (task
    sources, tests, log mountpoints) out of the layer's build context —
    everything the layer copies has to be re-included by name.

    A :class:`~tolokaforge_coding_harnesses.SkillDelivery` may
    append to either file afterwards; :class:`ImageLayerSkillDelivery` does.
    """
    harness_dir = staging_dir / _HARNESS_STAGING_DIR
    harness_dir.mkdir(exist_ok=True)
    shutil.copy2(INSTALL_SCRIPT, harness_dir / INSTALL_SCRIPT.name)

    install_script_path = f"{_HARNESS_STAGING_DIR}/{INSTALL_SCRIPT.name}"
    dockerfile = [
        f"FROM {base_image}",
        f"COPY {install_script_path} {_HARNESS_INSTALL_PATH}",
        f"RUN sh {_HARNESS_INSTALL_PATH} {spec.install_method} "
        f"{shlex.quote(spec.install_source)} {shlex.quote(spec.version)}",
    ]
    dockerignore_lines = [f"!{install_script_path}"]

    # Ship the middleware proxy alongside the install script when the harness
    # declares one. `python3` is present on every task base image we drive
    # (all inherit `python:*-slim-*`), and the proxy is pure stdlib — no
    # additional install step needed.
    if spec.request_middleware is not None:
        shutil.copy2(MIDDLEWARE_PROXY_SCRIPT, harness_dir / MIDDLEWARE_PROXY_SCRIPT.name)
        middleware_staging_path = f"{_HARNESS_STAGING_DIR}/{MIDDLEWARE_PROXY_SCRIPT.name}"
        dockerfile.append(f"COPY {middleware_staging_path} {MIDDLEWARE_PROXY_CONTAINER_PATH}")
        dockerignore_lines.append(f"!{middleware_staging_path}")

    (harness_dir / _HARNESS_DOCKERFILE_NAME).write_text("\n".join(dockerfile) + "\n")
    (staging_dir / ".dockerignore").write_text("*\n" + "\n".join(dockerignore_lines) + "\n")


@dataclass(frozen=True)
class ImageLayerSkillDelivery:
    """Deliver the bundle as one more layer on the harness image.

    Appends a ``COPY`` to the generated ``_harness/harness.Dockerfile`` and the
    matching exceptions to the staging ``.dockerignore``, after the CLI install
    — so editing a bundle invalidates the copy layer without reinstalling the
    CLI. The staging layout is therefore part of this implementation's
    contract, and of no other's: a delivery that uploads to a running sandbox
    ignores it entirely.
    """

    def deliver(self, bundle: SkillsBundle) -> None:
        construct = PATH_CONSTRUCT_PATTERN.search(bundle.target)
        if construct is not None:
            raise ValueError(
                f"terminal-bench adapter: skills_dir_target {bundle.target!r} still "
                f"carries {construct.group(0)!r} after the run's PathResolver ran. Docker "
                "expands a `COPY` destination from the image's own `ENV`, which is neither "
                "the resolver's answer nor the container shell's, so the bundle would land "
                "where the CLI does not look while the trial still recorded it. Name the "
                "variable in the resolver's vocabulary, or write the target absolute."
            )
        source = os.path.normpath(bundle.source_rel)
        dockerfile = bundle.staging_dir / _HARNESS_STAGING_DIR / _HARNESS_DOCKERFILE_NAME
        with dockerfile.open("a") as handle:
            handle.write(f"COPY {source}/. {bundle.target}\n")
        with (bundle.staging_dir / ".dockerignore").open("a") as handle:
            handle.write(f"!{source}\n!{source}/**\n")


DEFAULT_SKILL_DELIVERY: Final[SkillDelivery] = ImageLayerSkillDelivery()
"""The delivery every adapter surface falls back to when a caller names none."""


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
    harness_spec: HarnessSpec | None,
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
    if harness_spec is None:
        agent_image = base_image
    else:
        agent_image = f"{base_image}-{agent_harness}-{harness_spec.version}"
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
    if harness_spec is not None:
        # Static per-harness env — hardening flags the CLI reads at start-up
        # (``IS_SANDBOX=1`` for claude-code's root-user bypass, etc.). Written
        # into the compose ``environment:`` block so ``docker exec`` inherits
        # them. See :attr:`HarnessSpec.container_env`.
        for key, value in sorted(harness_spec.container_env.items()):
            agent_body["environment"] = _set_env_key(agent_body["environment"], key, value)

    base_build_service: str | None = None
    if harness_spec is not None:
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
