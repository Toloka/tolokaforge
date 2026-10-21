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
#   3. runtime — copies only the venv, so git/curl/perl, the
#      pip/setuptools/wheel toolchain and the constraints file never reach the
#      shipped image.
#
# Every stage that runs pip installs under ``image-constraints.txt``, the
# ``uv.lock`` export: ``PIP_CONSTRAINT`` pins each third-party package and the
# build backend of any sdist pip builds (pip < 25.3 applies it to isolated build
# environments, ``PIP_BUILD_CONSTRAINT`` covers pip >= 25.3). Every install is
# wheel-only except the lock's sdist-only packages (``--only-binary :all:
# --no-binary sly``, in that order — pip applies them left to right), and
# ``locked_install_check.py`` then fails the build if any installed distribution
# is off the lock or a first-party one does not match the local wheel it was
# installed from. Only the builder stages see pip and the constraints file.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# wheel-builder — build the runner-subset wheel from the source tree
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

# ``hatchling build --target custom`` runs the custom builder script directly
# in the current interpreter — no build-isolation venv, no ``hatch`` env
# machinery — so the layer is self-contained and reproducible.
RUN pip install --no-cache-dir --only-binary :all: --no-binary sly "hatchling>=1.24,<2.0" \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip

# Source files needed by ``hatchling build --target custom``. Copy pyproject
# and metadata files first (rarely change) so the layer that ADDs the
# tolokaforge source cache-invalidates independently.
COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/
# The models wheel is a separate PEP 621 project (workspace sibling). Its
# source ships in the same context so we can build it here rather than
# pulling from PyPI — which is essential before the first
# ``models-vX.Y.Z`` publish, and remains preferable after (Docker installs
# the freshly-built wheel from the checked-out tree, not whatever PyPI
# currently ships).
COPY tolokaforge_models/ /src/tolokaforge_models/
# tolokaforge_coding_harnesses is a workspace sibling that ships INSIDE the
# base tolokaforge wheel on PyPI (see ``[tool.hatch.build.targets.wheel]``
# in the workspace pyproject) — never as its own PyPI distribution. The
# runner subset wheel uses a separate companion wheel built here in-container,
# so the source tree ships in this build context alongside tolokaforge_models.
COPY tolokaforge_coding_harnesses/ /src/tolokaforge_coding_harnesses/

# Build the subset wheel. Output lands in ``/src/dist/`` as
# ``tolokaforge_runner_subset-<version>-py3-none-any.whl``.
RUN python -m hatchling build --target custom

# Build the models wheel in-place. Output lands under
# ``/src/tolokaforge_models/dist/`` as
# ``tolokaforge_models-<version>-py3-none-any.whl``.
RUN cd /src/tolokaforge_models && python -m hatchling build

# Same for the coding-harnesses wheel. Output under
# ``/src/tolokaforge_coding_harnesses/dist/`` as
# ``tolokaforge_coding_harnesses-<version>-py3-none-any.whl``.
RUN cd /src/tolokaforge_coding_harnesses && python -m hatchling build

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

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

COPY --from=wheel-builder /src/dist/tolokaforge_runner_subset-*.whl /tmp/
COPY --from=wheel-builder /src/tolokaforge_models/dist/tolokaforge_models-*.whl /tmp/
COPY --from=wheel-builder /src/tolokaforge_coding_harnesses/dist/tolokaforge_coding_harnesses-*.whl /tmp/

# Install into an isolated venv. The subset wheel's METADATA carries every
# runtime dep (the base wheel deps the runner reaches + the former
# ``[runner]`` extra) plus ``tolokaforge-models>=1.0.0,<2.0.0``; the
# freshly-built models wheel from the wheel-builder stage satisfies that pin
# without touching PyPI. tolokaforge-coding-harnesses is installed alongside
# from its own freshly-built wheel because the runner subset imports
# ``tolokaforge_coding_harnesses`` at boot and it never ships on PyPI (bundled
# INTO the base tolokaforge wheel for PyPI users). --no-compile keeps *.pyc
# bytecode out of site-packages; PYTHONDONTWRITEBYTECODE in the runtime stage
# keeps it that way.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile --only-binary :all: --no-binary sly \
        /tmp/tolokaforge_models-*.whl \
        /tmp/tolokaforge_coding_harnesses-*.whl \
        /tmp/tolokaforge_runner_subset-*.whl \
    && /opt/venv/bin/python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt \
        --first-party tolokaforge-runner-subset \
        --first-party tolokaforge-models \
        --first-party tolokaforge-coding-harnesses \
        --exempt pip

# Playwright for the browser tool (opt-in via build arg), auto-set by the
# orchestrator when a task enables the browser/mobile tools. Installed here,
# where pip, the constraints and the first-party wheels the guard verifies
# against are present; the runtime stage installs only Chromium.
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    /opt/venv/bin/pip install --no-cache-dir --no-compile --only-binary :all: --no-binary sly \
        playwright \
    && /opt/venv/bin/python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt \
        --first-party tolokaforge-runner-subset \
        --first-party tolokaforge-models \
        --first-party tolokaforge-coding-harnesses \
        --exempt pip; \
    fi

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

# Chromium for the Playwright package the builder stage installed.
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    playwright install --with-deps chromium; \
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

# Strip the install toolchain — the runtime never installs packages, so it
# carries neither pip nor the constraints file. Reclaims the pip/setuptools/wheel
# footprint the venv seeded at creation time.
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
