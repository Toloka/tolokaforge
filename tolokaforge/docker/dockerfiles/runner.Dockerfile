# Runner Container - Tool execution + grading
#
# This container runs the Runner gRPC service that handles:
# - Trial registration with TaskDescription
# - Tool execution (MCP server styles, terminal-bench)
# - Trial grading via golden path comparison
#
# tolokaforge is installed from a pre-built wheel placed into the build
# context by the host-side wheel resolver (tolokaforge.docker.wheel_resolver).
# The container never clones a repo or reaches PyPI — the wheel is local.
#
# Multi-stage: the builder installs the wheel + its [runner] extra into an
# isolated venv (with the build-only apt toolchain); the runtime stage copies
# only that venv, so git/curl/perl and the pip/setuptools/wheel toolchain never
# reach the shipped image.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# builder — install the wheel + [runner] extra into /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# Build-only system deps. Wheels that carry native build steps need a compiler
# toolchain; git/curl are here for any source build the resolver's deps trigger.
# None of this reaches the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# WHEEL_FILENAME is passed as a build arg by the wheel resolver so we don't rely
# on shell glob expansion inside Docker. No default — a missing --build-arg
# fails loudly at this layer rather than silently COPYing the wrong filename.
ARG WHEEL_FILENAME
COPY ${WHEEL_FILENAME} /tmp/

# Install into an isolated venv. The [runner] extra is the single source of
# truth for the runner image's domain-tool runtime deps (declared in
# pyproject.toml). --no-compile keeps *.pyc bytecode out of site-packages;
# PYTHONDONTWRITEBYTECODE in the runtime stage keeps it that way.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile "/tmp/${WHEEL_FILENAME}[runner]" \
    && rm -f "/tmp/${WHEEL_FILENAME}"

# ---------------------------------------------------------------------------
# runtime — copy only the venv; no build toolchain
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# ca-certificates only — the runner opens TLS connections to LLM APIs for
# in-container LLM-as-judge grading. No git/curl: the runner never clones.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Playwright for browser tool (opt-in via build arg). Auto-set by the
# orchestrator when a task enables the browser/mobile tools. Runs before the
# toolchain strip below because it uses pip.
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    pip install playwright && playwright install --with-deps chromium; \
    fi

# Docker CLI + Compose plugin (opt-in via build arg). No Docker daemon — uses
# the host daemon via a mounted /var/run/docker.sock. Auto-set by the
# orchestrator when the run uses the terminal-bench adapter, which shells out
# to docker inside the runner; every other run ships without it.
ARG INSTALL_DOCKER_CLI=false
RUN if [ "$INSTALL_DOCKER_CLI" = "true" ]; then \
    apt-get update \
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
    && rm -rf /var/lib/apt/lists/*; \
    fi

# Strip the install toolchain — the runtime never installs packages. Reclaims
# the pip/setuptools/wheel footprint the venv seeded at creation time. Last, so
# the opt-in pip path above still has pip available.
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

# Domain code is delivered at runtime via TaskDescription.tool_artifacts —
# adapters bundle the env/domain tree there, the runner extracts it under a
# per-trial tempdir and prepends it to sys.path. The image stays domain-agnostic;
# the [runner] extra carries the drivers that extracted tool code needs.

# Create work directory for tool execution
RUN mkdir -p /work && chmod 755 /work

# Environment variables.
# Service URLs are NOT baked here — the stacks inject them so the container env
# reflects what is actually running:
#   * DB_SERVICE_URL — every stack runs db-service and injects it
#     (docker/stacks/{core,test}.py); runner/__main__.py keeps a localhost
#     default for bare/local runs (a missing/wrong DB URL fails loud on first
#     call, so a default is safe here — unlike RAG).
#   * RAG_SERVICE_URL — intentionally absent: it must be present iff a
#     rag-service is actually running. Only the full stack injects it
#     (docker/stacks/full.py); the core stack leaves it unset so the runner
#     builds no RAG client and the judge is offered no unreachable search_kb.

# gRPC port
EXPOSE 50051

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=2)" || exit 1

# Run the Runner service
CMD ["python", "-m", "tolokaforge.runner"]
