# Trial Node Container — a node that owns one whole trial.
#
# Ships the tolokaforge trial node (see docs/TRIAL_NODE_SERVICE.md) as an
# independently-deployable image. The engine's ``--runtime remote`` ships a
# trial's inputs to one of these over the ``RunTrial`` RPC; the node runs the
# conductor and the agent loop itself, brings up per-trial stacks over the
# mounted docker socket, and dials a co-located runner at EXECUTOR_ADDRESS for
# tasks without a per-trial stack. Unlike the runner image,
# it needs the whole ``tolokaforge`` distribution (orchestration, adapters,
# conductor) rather than the runner subset wheel, plus the Docker CLI.
#
# Deployment contract:
#   * mount the host Docker socket at /var/run/docker.sock;
#   * pre-provision ``tolokaforge-runner:local`` and
#     ``tolokaforge-db-service:local`` on that daemon — the node builds no
#     images (``make docker-build-core`` at node bootstrap, or pull and tag);
#   * pass the node's credentials as TOLOKAFORGE_SECRETS_JSON — trial inputs
#     carry ``${secret:NAME}`` references only;
#   * set TOLOKAFORGE_GRPC_MAX_MESSAGE_MB to at least the engine's
#     ``compute.grpc_max_message_mb``;
#   * optionally mount task packs under TRIAL_NODE_TASK_PACK_ROOT, one
#     directory per task id.
#
# The container command is ``python -m tolokaforge.trial_node``; it reads
# ``--port`` (or TRIAL_NODE_PORT, default 50061).

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# wheel-builder — build the base wheel + models wheel from source
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"

COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/
COPY tolokaforge_models/ /src/tolokaforge_models/
COPY tolokaforge_coding_harnesses/ /src/tolokaforge_coding_harnesses/

# Build the base wheel — the node ships the whole tolokaforge distribution,
# tolokaforge_models and tolokaforge_coding_harnesses included.
RUN python -m hatchling build --target wheel && \
    cd /src/tolokaforge_models && python -m hatchling build && \
    cd /src/tolokaforge_coding_harnesses && python -m hatchling build

# ---------------------------------------------------------------------------
# builder — install the wheel into /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY --from=wheel-builder /src/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_coding_harnesses/dist/*.whl /tmp/wheels/

# Install the sibling wheels first so their versions resolve before the
# base wheel that depends on them (matching the runner image install order).
RUN /opt/venv/bin/pip install --no-cache-dir \
        /tmp/wheels/tolokaforge_models-*.whl \
        /tmp/wheels/tolokaforge_coding_harnesses-*.whl && \
    /opt/venv/bin/pip install --no-cache-dir /tmp/wheels/tolokaforge-*.whl

# ---------------------------------------------------------------------------
# runtime — copy only the venv; strip pip/setuptools/wheel for image size
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# Docker CLI + Compose plugin: no daemon — the node drives the host daemon
# through the mounted /var/run/docker.sock to bring up each trial's stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
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
    && rm -rf /var/lib/apt/lists/* && \
    apt-get clean

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TRIAL_NODE_PORT=50061

# Strip the packaging toolchain; the runtime image is a service, not a builder.
RUN /opt/venv/bin/pip uninstall -y pip setuptools wheel

EXPOSE 50061

# gRPC has no HTTP endpoint and the image carries no grpc health probe, so
# readiness is a TCP connect to the service port.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import socket, sys; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 50061)); s.close(); sys.exit(0)" || exit 1

CMD ["python", "-m", "tolokaforge.trial_node"]
