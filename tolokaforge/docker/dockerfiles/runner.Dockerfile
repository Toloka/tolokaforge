# Runner Container - Tool execution + grading
#
# This container runs the Runner gRPC service that handles:
# - Trial registration with TaskDescription
# - Tool execution (MCP server styles, terminal-bench)
# - Trial grading via golden path comparison
#
# tolokaforge is installed from the runner-subset wheel, a Docker-only build
# artifact never published to PyPI. The subset packages the runner's runtime
# import closure — orchestrator, adapters, dx, docker helpers, and env
# services stay in the base wheel. ADR-0025 owns the split; ADR-0024 owns
# the container command surface, preserved here verbatim.
#
# Three stages:
#
#   1. wheel-builder — installs hatch, copies the source tree hatch needs,
#      and runs ``hatch build --target custom`` to produce the subset wheel
#      under ``/src/dist/``. The custom builder lives at
#      ``scripts/hatch/hatch_runner_subset_builder.py`` and consumes the
#      partition enumerated in ``tolokaforge/core/_runner_subset.py``.
#   2. builder — copies the subset wheel from wheel-builder and installs it
#      into an isolated ``/opt/venv`` (with any build-only apt toolchain).
#      The subset wheel's METADATA declares the runner's runtime deps (the
#      union of the base wheel's ``[project.dependencies]`` reachable from
#      the runner subset and the domain-tool ``[runner]`` extra) so no
#      extras selector is needed at install time.
#   3. runtime — copies only the venv, so git/curl/perl and the
#      pip/setuptools/wheel toolchain never reach the shipped image.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# wheel-builder — build the runner-subset wheel from the source tree
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

# ``hatchling build --target custom`` runs the custom builder script directly
# in the current interpreter — no build-isolation venv, no ``hatch`` env
# machinery — so the layer is self-contained and reproducible. Version-bound
# so the produced wheel metadata format is deterministic across builds.
RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"

# Source files needed by ``hatchling build --target custom``. Copy pyproject
# and metadata files first (rarely change) so the layer that ADDs the
# tolokaforge source cache-invalidates independently.
COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/

# Build the subset wheel. Output lands in ``/src/dist/`` as
# ``tolokaforge_runner_subset-<version>-py3-none-any.whl``.
RUN python -m hatchling build --target custom

# ---------------------------------------------------------------------------
# builder — install the subset wheel into /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# Build-only system deps. Wheels that carry native build steps need a compiler
# toolchain; git/curl are here for any source build a subset dep triggers.
# None of this reaches the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheel-builder /src/dist/tolokaforge_runner_subset-*.whl /tmp/

# Install into an isolated venv. The subset wheel's METADATA carries every
# runtime dep (the base wheel deps the runner reaches + the former
# ``[runner]`` extra), so no extras selector is needed. --no-compile keeps
# *.pyc bytecode out of site-packages; PYTHONDONTWRITEBYTECODE in the
# runtime stage keeps it that way.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile /tmp/tolokaforge_runner_subset-*.whl \
    && rm -f /tmp/tolokaforge_runner_subset-*.whl

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
# the subset wheel carries the drivers that extracted tool code needs.

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
