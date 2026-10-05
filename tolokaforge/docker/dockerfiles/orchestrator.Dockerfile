# Orchestrator Container — full ``tolokaforge run`` batch driver.
#
# Ships the whole engine on a clean machine so an operator with only Docker and
# provider credentials can drive a batch end to end:
#
#     docker run ... <image> run --config /work/config/<config>.yaml
#
# This is NOT the single-trial driver in ``deploy/standalone/`` — that stands up
# the runner stack for one any-language trial. This image runs the complete
# ``tolokaforge run`` orchestration loop: it reads a run config, materialises a
# docker-compose stack per the runtime backend, and shells out to ``docker
# compose`` (see ``tolokaforge/core/compose_materialisation.py``). Those child
# containers must be siblings of this one on the host daemon, so the image
# ships the Docker CLI + Compose plugin and talks to the host daemon over a
# mounted ``/var/run/docker.sock`` (Docker-out-of-Docker). It carries NO Docker
# daemon of its own.
#
# The default install is the engine plus the ``[dx]`` extra — the CLI shim
# (``tolokaforge._entry:main``) prints an install hint and exits non-zero
# without it. The default ``[dx]`` image runs the native backend; terminal_bench
# and the other adapters ship as separate distributions and are opt-in via
# ``--build-arg EXTRAS=dx,adapters``.
#
# Secrets never enter the image. They reach a running container only through a
# mounted read-only ``.env`` (consumed by ``DotEnvProvider``) — never a
# build-arg, never baked into a layer.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# sibling-wheel-builder — build the workspace-sibling wheel the base wheel
# depends on (``tolokaforge_models``). ``tolokaforge_coding_harnesses`` is
# bundled INTO the base wheel (see ``[tool.hatch.build.targets.wheel]`` in the
# workspace pyproject), so only the models wheel is a separate dependency.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS sibling-wheel-builder

WORKDIR /src

RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"

COPY tolokaforge_models/ /src/tolokaforge_models/

RUN cd /src/tolokaforge_models && python -m hatchling build

# ---------------------------------------------------------------------------
# builder — install the staged base wheel (with the selected extras) plus the
# freshly-built models wheel into an isolated ``/opt/venv``.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# Build-only system deps: a compiler toolchain for any dependency that carries
# a native build step, git/curl for a source build a dep might trigger. None of
# this reaches the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The base wheel is produced on the host (``uv build``) and staged into the
# build context by the build script; WHEEL_FILENAME has no default so a missing
# --build-arg fails loudly at the COPY below rather than copying the wrong file.
ARG WHEEL_FILENAME
# EXTRAS selects the wheel's optional-dependency groups. The default ``dx`` is
# the engine + terminal front-end (required — the CLI shim refuses to run
# without it). ``dx,adapters`` pulls the opt-in adapter packages; those resolve
# from their own distributions, so an offline / unpublished build may need the
# adapters built from source instead (see docs/ORCHESTRATOR_IMAGE.md).
ARG EXTRAS=dx

COPY ${WHEEL_FILENAME} /tmp/
COPY --from=sibling-wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/

# Install the sibling wheel first so its version resolves before the base wheel
# that depends on it. ``--no-compile`` keeps *.pyc out of site-packages;
# PYTHONDONTWRITEBYTECODE in the runtime stage keeps it that way.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile /tmp/wheels/tolokaforge_models-*.whl \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile "/tmp/${WHEEL_FILENAME}[${EXTRAS}]" \
    && rm -f "/tmp/${WHEEL_FILENAME}" /tmp/wheels/*.whl

# ---------------------------------------------------------------------------
# runtime — copy only the venv; add the Docker CLI + Compose plugin (no daemon)
# so the engine can shell out to ``docker compose`` against the mounted host
# socket. No build toolchain reaches the shipped image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Docker CLI + Compose plugin. No Docker daemon — the engine drives the HOST
# daemon via a mounted /var/run/docker.sock (Docker-out-of-Docker), so child
# compose stacks come up as host siblings with host-published ports. Same apt
# recipe as runner.Dockerfile's INSTALL_DOCKER_CLI block. ca-certificates stays
# for the TLS the engine opens to provider APIs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/etc/apt/keyrings/docker.asc] \
    https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Strip the install toolchain — the runtime never installs packages. Reclaims
# the pip/setuptools/wheel footprint the venv seeded at creation time.
RUN rm -rf /opt/venv/lib/python*/site-packages/pip \
    /opt/venv/lib/python*/site-packages/pip-*.dist-info \
    /opt/venv/lib/python*/site-packages/setuptools \
    /opt/venv/lib/python*/site-packages/setuptools-*.dist-info \
    /opt/venv/lib/python*/site-packages/pkg_resources \
    /opt/venv/lib/python*/site-packages/_distutils_hack \
    /opt/venv/lib/python*/site-packages/distutils-precedence.pth \
    /opt/venv/lib/python*/site-packages/wheel \
    /opt/venv/lib/python*/site-packages/wheel-*.dist-info \
    /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
    /opt/venv/bin/wheel

# Non-root user. Reaching the mounted host /var/run/docker.sock as this user
# needs the socket's group added at run time:
# ``--group-add "$(stat -c %g /var/run/docker.sock)"`` on Linux (see
# scripts/docker/run-orchestrator.sh and docs/ORCHESTRATOR_IMAGE.md).
RUN useradd --create-home --uid 1000 runner \
    && mkdir -p /work \
    && chown runner:runner /work

WORKDIR /work
USER runner

# ``docker run <img> run --config ...`` and ``docker run <img> --help`` both
# work: the entrypoint is the CLI, the default command prints help.
ENTRYPOINT ["tolokaforge"]
CMD ["--help"]
