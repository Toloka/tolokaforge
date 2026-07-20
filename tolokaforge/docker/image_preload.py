"""Best-effort preloading of host Docker images into a DinD sidecar."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import docker
import yaml
from docker.errors import ImageNotFound

if TYPE_CHECKING:
    from tolokaforge.docker.container import Container

logger = logging.getLogger(__name__)


def discover_image_tags(
    compose_files: list[Path],
    extra_images: list[str],
) -> list[str]:
    """Return unique Compose service image tags followed by explicit tags.

    Each Compose file is isolated so one unreadable or malformed task cannot
    prevent images from other tasks from being considered for preloading.
    """
    tags: list[str] = []

    for compose_file in compose_files:
        try:
            payload = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
            services = payload.get("services") if isinstance(payload, dict) else None
            if not isinstance(services, dict):
                raise ValueError("top-level 'services' must be a mapping")

            for service_name, service in services.items():
                if not isinstance(service, dict) or "image" not in service:
                    continue
                image = service["image"]
                if not isinstance(image, str) or not image.strip():
                    logger.warning(
                        "Ignoring invalid image for service %r in %s: %r",
                        service_name,
                        compose_file,
                        image,
                    )
                    continue
                tag = image.strip()
                if tag not in tags:
                    tags.append(tag)
        except Exception as exc:
            logger.warning("Could not discover images from %s: %s", compose_file, exc)

    for image in extra_images:
        try:
            tag = image.strip()
            if not tag:
                raise ValueError("image tag must not be empty")
            if tag not in tags:
                tags.append(tag)
        except Exception as exc:
            logger.warning("Could not use explicit preload image %r: %s", image, exc)

    return tags


def wait_for_dind_daemon(
    dind_container: Container,
    dind_endpoint: str,
    *,
    timeout_s: float,
    interval_s: float,
) -> None:
    """Wait until dockerd in the already-running DinD container responds."""
    deadline = time.monotonic() + timeout_s
    last_error = "Docker daemon did not respond"
    command = ["docker", "--host", dind_endpoint, "info"]

    while True:
        try:
            result = dind_container.exec(command)
            if result.exit_code == 0:
                logger.info("DinD daemon is healthy at %s", dind_endpoint)
                return
            last_error = result.stderr or result.stdout or f"exit code {result.exit_code}"
        except Exception as exc:
            last_error = str(exc)

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"DinD daemon at {dind_endpoint} was not ready after {timeout_s:.1f}s: {last_error}"
            )
        time.sleep(interval_s)


def _stream_image_to_dind(
    tag: str,
    *,
    dind_container_name: str,
    dind_endpoint: str,
) -> None:
    """Stream ``docker save`` into ``docker load`` inside the DinD container."""
    save_command = ["docker", "save", tag]
    load_command = [
        "docker",
        "exec",
        "-i",
        dind_container_name,
        "docker",
        "--host",
        dind_endpoint,
        "load",
    ]

    with (
        tempfile.TemporaryFile() as save_stderr,
        tempfile.TemporaryFile() as load_stdout,
        tempfile.TemporaryFile() as load_stderr,
    ):
        save_process = subprocess.Popen(
            save_command,
            stdout=subprocess.PIPE,
            stderr=save_stderr,
        )
        if save_process.stdout is None:
            save_process.kill()
            save_process.wait()
            raise RuntimeError(f"docker save did not expose a stream for {tag}")

        try:
            load_process = subprocess.Popen(
                load_command,
                stdin=save_process.stdout,
                stdout=load_stdout,
                stderr=load_stderr,
            )
        except Exception:
            save_process.stdout.close()
            save_process.kill()
            save_process.wait()
            raise

        save_process.stdout.close()
        load_returncode = load_process.wait()
        save_returncode = save_process.wait()

        save_stderr.seek(0)
        load_stdout.seek(0)
        load_stderr.seek(0)
        save_error = save_stderr.read().decode("utf-8", errors="replace").strip()
        load_output = load_stdout.read().decode("utf-8", errors="replace").strip()
        load_error = load_stderr.read().decode("utf-8", errors="replace").strip()

    if save_returncode != 0:
        raise RuntimeError(f"docker save failed for {tag}: {save_error or save_returncode}")
    if load_returncode != 0:
        detail = load_error or load_output or str(load_returncode)
        raise RuntimeError(f"docker load failed for {tag}: {detail}")


def preload_images_into_dind(
    tags: list[str],
    *,
    dind_container_name: str,
    dind_endpoint: str,
    host_client: Any | None = None,
) -> None:
    """Load host-present tags into DinD, warning and continuing per image."""
    try:
        client = host_client if host_client is not None else docker.from_env()
    except Exception as exc:
        logger.warning("Could not connect to the host Docker daemon for image preload: %s", exc)
        return

    for tag in tags:
        try:
            client.images.get(tag)
        except ImageNotFound:
            logger.warning(
                "Task image %s is absent from the host Docker daemon; DinD will build it on use",
                tag,
            )
            continue
        except Exception as exc:
            logger.warning("Could not check host Docker image %s; skipping preload: %s", tag, exc)
            continue

        started_at = time.monotonic()
        try:
            _stream_image_to_dind(
                tag,
                dind_container_name=dind_container_name,
                dind_endpoint=dind_endpoint,
            )
        except Exception as exc:
            logger.warning(
                "Failed to preload task image %s into DinD after %.2fs; DinD will build it on "
                "use: %s",
                tag,
                time.monotonic() - started_at,
                exc,
            )
            continue

        logger.info(
            "Preloaded task image %s into DinD at %s in %.2fs",
            tag,
            dind_endpoint,
            time.monotonic() - started_at,
        )
