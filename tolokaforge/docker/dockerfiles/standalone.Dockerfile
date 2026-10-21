# All-in-one (single-image, multi-service) tolokaforge image.
#
# Runs the whole runner stack — db-service (8000), rag-service (8001),
# mock-web (8080), and the runner gRPC service (50051) — inside ONE container,
# supervised by supervisord. The five per-component images published from this
# repo stay the distributed/scale-out shape; this image is the "just runs
# anywhere Docker does" shape. See docs/adr/0056-all-in-one-image.md.
#
# NB "all-in-one" / "single-image multi-service", never "monolith": ADR-0023/
# 0025 already use "monolithic" for the runner image's full-wheel *internal*
# composition, a different axis.
#
# Grading runs in-process on the runner via the aggregate path (grader.name:
# runner_rpc + InProcessGradingSubstrate, ADR-0040) — no separate grader
# process. The detached grader stays available via the tolokaforge-grader
# image for distributed deployments.
#
# Add-ons work like PyPI extras, because inside they ARE extras. The lean base
# installs tolokaforge[dx,server,rag,runner]; pip is deliberately retained in
# the runtime so a heavier image is one layer away:
#
#   FROM tolokasoft1/tolokaforge-standalone:<ver>
#   RUN /opt/venv/bin/pip install 'tolokaforge[browser]' \
#       && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/venv/bin/playwright \
#          install --with-deps chromium
#
# Every install here runs under image-constraints.txt, the uv.lock export, and
# is checked against it (see the runner.Dockerfile header). The runtime keeps
# PIP_CONSTRAINT and PIP_BUILD_CONSTRAINT pointing at it, so a derived image's
# `pip install` pins every third-party package the lock pins. The constraints
# hold no first-party distribution: a derived `pip install` takes the
# terminal-bench adapter from an index, unpinned, and finds no
# tolokaforge-langfuse on PyPI at all. A package the lock does not list also
# resolves live, and `uv pip` reads neither variable.
# Opt out per command with `PIP_CONSTRAINT= PIP_BUILD_CONSTRAINT= pip install ...`.
#
# A custom TOLOKAFORGE_EXTRAS is the way to get the first-party plugins pinned
# and local: it builds pinned to the lock like the default, with first-party
# wheels built from this image's own source — `otel` installs the langfuse
# plugin wheel and `adapters` / `terminal_bench` / `all` the terminal-bench
# adapter wheel, both built in the wheel-builder stage. `browser` installs the
# Python package only; Chromium needs INSTALL_BROWSER=true.
#
# The INSTALL_BROWSER / INSTALL_DOCKER_CLI / INSTALL_TYPESENSE build args bake
# the heavy add-ons at build time (same opt-in pattern as runner.Dockerfile).
# INSTALL_TYPESENSE bundles a co-located TypeSense server so the image can serve
# search-backed tasks (e.g. the ots packs). The Dockerfile is arch-agnostic, so
# the image publishes multi-arch (linux/amd64 + linux/arm64); the TypeSense
# flavor in particular needs the native arch (its raft writes stall under qemu).
#
# Three stages: wheel-builder (base + sibling + plugin wheels from source, like
# grader.Dockerfile) -> builder (one shared /opt/venv: the rag search deps on
# CPU torch and the baked embedding model first, then the base wheel with its
# extras) -> runtime (copy the venv, wire the four services under supervisord).

ARG PYTHON_VERSION=3.12
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# ---------------------------------------------------------------------------
# wheel-builder — build the base wheel + sibling wheels from source
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

RUN pip install --no-cache-dir --only-binary :all: --no-binary sly "hatchling>=1.24,<2.0" \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip

COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/
COPY tolokaforge_models/ /src/tolokaforge_models/
COPY tolokaforge_coding_harnesses/ /src/tolokaforge_coding_harnesses/
COPY tolokaforge_langfuse/ /src/tolokaforge_langfuse/
COPY external_adapters/tolokaforge-adapter-terminal-bench/ /src/external_adapters/tolokaforge-adapter-terminal-bench/

# Ship the whole tolokaforge distribution, models + coding-harnesses included
# (same as grader.Dockerfile — the all-in-one image needs the orchestrator,
# runner, env services, adapters, and dx front-end all present). The langfuse
# and adapter wheels are what the `otel` and `adapters` extras require; the
# builder installs them only when TOLOKAFORGE_EXTRAS asks.
RUN python -m hatchling build --target wheel && \
    cd /src/tolokaforge_models && python -m hatchling build && \
    cd /src/tolokaforge_coding_harnesses && python -m hatchling build && \
    cd /src/tolokaforge_langfuse && python -m hatchling build && \
    cd /src/external_adapters/tolokaforge-adapter-terminal-bench && python -m hatchling build

# ---------------------------------------------------------------------------
# builder — one shared venv for all four services
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# Build-only toolchain — never reaches the runtime stage (which copies only
# the venv). Covers any sdist-only transitive dep that needs a compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

# The rag-service search stack (its requirements.txt is the SSOT) on CPU torch,
# layered above every layer carrying tolokaforge source so a source edit
# re-downloads neither torch nor the embedding model. CPU torch goes first,
# alone and without its dependencies, from the CPU index at the locked `+cpu`
# version, so the multi-GB CUDA build is never pulled — the single biggest size
# win for the all-in-one image. The next install resolves torch's dependencies
# from PyPI under the lock.
COPY tolokaforge/env/rag_service/requirements.txt /tmp/rag-requirements.txt
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --only-binary :all: --no-binary sly \
        --no-deps --index-url https://download.pytorch.org/whl/cpu torch && \
    /opt/venv/bin/pip install --no-cache-dir --only-binary :all: --no-binary sly \
        -r /tmp/rag-requirements.txt && \
    /opt/venv/bin/python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip && \
    /opt/venv/bin/pip check && \
    /opt/venv/bin/python -c "import torch; assert torch.__version__.endswith('+cpu'), torch.__version__"

# Bake the embedding model into the venv image so the rag-service stands up
# with no HuggingFace round-trip. Must run with network on (no HF_HUB_OFFLINE
# yet); the runtime stage sets HF_HUB_OFFLINE=1 so the baked weights are the
# ones served.
ARG EMBEDDING_MODEL
ENV HF_HOME=/opt/hf-cache
RUN /opt/venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

COPY --from=wheel-builder /src/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_coding_harnesses/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_langfuse/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/external_adapters/tolokaforge-adapter-terminal-bench/dist/*.whl /tmp/wheels/

# Lean-base extras, comma-separated. Override with --build-arg
# TOLOKAFORGE_EXTRAS=... to add office/adapters/otel: this is how otel and
# adapters get their first-party wheels from this image's source, which a
# post-build `pip install` cannot (tolokaforge-langfuse is not on PyPI). Declared
# here so the layers above are shared by every extras value.
ARG TOLOKAFORGE_EXTRAS="dx,server,rag,runner"

# One install of the base wheel with its extras, the local first-party wheels
# those extras require (installed by path, so no index copy can stand in for
# them) and the supervisor. The /tmp/wheels files stay: the guard compares each
# first-party install with its wheel, and this stage never ships.
RUN BASE_WHL="$(ls /tmp/wheels/tolokaforge-*.whl)" && \
    PLUGIN_WHEELS="" && \
    case ",${TOLOKAFORGE_EXTRAS}," in *,otel,*) \
        PLUGIN_WHEELS="${PLUGIN_WHEELS} $(ls /tmp/wheels/tolokaforge_langfuse-*.whl)" ;; esac && \
    case ",${TOLOKAFORGE_EXTRAS}," in *,adapters,*|*,terminal_bench,*|*,all,*) \
        PLUGIN_WHEELS="${PLUGIN_WHEELS} $(ls /tmp/wheels/tolokaforge_adapter_terminal_bench-*.whl)" ;; esac && \
    /opt/venv/bin/pip install --no-cache-dir --only-binary :all: --no-binary sly \
        /tmp/wheels/tolokaforge_models-*.whl \
        /tmp/wheels/tolokaforge_coding_harnesses-*.whl \
        "${BASE_WHL}[${TOLOKAFORGE_EXTRAS}]" \
        ${PLUGIN_WHEELS} \
        "supervisor>=4.2,<5" && \
    /opt/venv/bin/pip check && \
    /opt/venv/bin/python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt \
        --first-party tolokaforge \
        --first-party tolokaforge-models \
        --first-party tolokaforge-coding-harnesses \
        --first-party tolokaforge-langfuse \
        --first-party tolokaforge-adapter-terminal-bench \
        --exempt pip

# Browser add-on (opt-in): the Playwright package installs here, under the
# constraints and the guard; the runtime stage installs Chromium.
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
    /opt/venv/bin/pip install --no-cache-dir --only-binary :all: --no-binary sly \
        'playwright>=1.40.0' 'pillow>=12.1.0' && \
    /opt/venv/bin/python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt \
        --first-party tolokaforge \
        --first-party tolokaforge-models \
        --first-party tolokaforge-coding-harnesses \
        --first-party tolokaforge-langfuse \
        --first-party tolokaforge-adapter-terminal-bench \
        --exempt pip; \
    fi

# ---------------------------------------------------------------------------
# runtime — copy the shared venv, wire the four services under supervisord
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG EMBEDDING_MODEL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    CORPUS_PATH=/env/rag/corpus \
    DB_SERVICE_URL=http://localhost:8000 \
    RAG_SERVICE_URL=http://localhost:8001 \
    RUNNER_PORT=50051

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Dedicated non-root service user. supervisord runs as pid 1 (root,
# so the entrypoint can add the loopback host aliases) and launches every
# service program as this user — the workloads never run as root.
RUN useradd --create-home --uid 10001 runner

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf-cache /opt/hf-cache

# Derived images' `pip install` runs under the constraints (see the header).
COPY tolokaforge/docker/image-constraints.txt /opt/tolokaforge/image-constraints.txt
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

# Each env service is a FastAPI app named `app` with no package __init__, run
# today as `uvicorn app:app` with the service dir as CWD. Give each its own
# app-dir so the three `app` modules don't collide in one interpreter path.
COPY tolokaforge/env/json_db_service/app.py /srv/db/app.py
COPY tolokaforge/env/rag_service/app.py /srv/rag/app.py
COPY tolokaforge/env/mock_web_service/app.py /srv/web/app.py

COPY tolokaforge/docker/dockerfiles/standalone/supervisord.conf /etc/supervisor/standalone.conf
COPY tolokaforge/docker/dockerfiles/standalone/entrypoint.sh /usr/local/bin/tolokaforge-standalone-entrypoint

# ``conf.d`` holds opt-in supervised programs added by the add-on build-arg
# blocks below (e.g. TypeSense). The base supervisord.conf ``[include]``s it; an
# empty dir is a valid no-op, so the lean image carries no extra programs.
RUN chmod +x /usr/local/bin/tolokaforge-standalone-entrypoint && \
    mkdir -p /env/rag /work /var/log/tolokaforge /etc/supervisor/conf.d && \
    chown -R runner:runner /srv /env /work /var/log/tolokaforge /opt/hf-cache

# Opt-in heavy add-ons, same pattern as runner.Dockerfile. pip is retained
# (not stripped) so these can also be layered post-build by a derived image —
# the PyPI-extras analog.
ARG INSTALL_BROWSER=false
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
    /opt/venv/bin/playwright install --with-deps chromium && \
    chown -R runner:runner /opt/pw-browsers; \
    fi

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

# TypeSense flavor (opt-in): bundles the TypeSense server as a co-located
# supervised process so the all-in-one image can serve search-backed tasks
# (e.g. the ots packs' frozen_mcp_core adapter) with no external search plane.
# Pinned to the same version the distributed stack uses
# (tolokaforge/docker/stacks/typesense.py); installed as the native binary
# rather than the typesense/typesense image since this is a single container.
# TYPESENSE_HOST/PORT are set so the co-located runner resolves the in-container
# server over loopback; the api key is NOT baked (the entrypoint defaults it and
# the orchestrator must use the same key — see deploy/single-container/README).
ARG INSTALL_TYPESENSE=false
ARG TYPESENSE_VERSION=26.0
RUN if [ "$INSTALL_TYPESENSE" = "true" ]; then \
    apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-${arch}.tar.gz" \
       -o /tmp/typesense.tar.gz \
    && tar -xzf /tmp/typesense.tar.gz -C /usr/local/bin typesense-server \
    && rm -f /tmp/typesense.tar.gz \
    && mkdir -p /env/typesense \
    && chown -R runner:runner /env/typesense \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*; \
    fi
COPY tolokaforge/docker/dockerfiles/standalone/typesense.conf /tmp/typesense.conf
RUN if [ "$INSTALL_TYPESENSE" = "true" ]; then \
    mv /tmp/typesense.conf /etc/supervisor/conf.d/typesense.conf; \
    else rm -f /tmp/typesense.conf; fi
ENV TYPESENSE_ENABLED=${INSTALL_TYPESENSE}

EXPOSE 8000 8001 8080 8108 50051

# Healthy once the runner's gRPC channel is ready (ADR-0024 surface). The
# runner starts last (supervisord priority), so a ready runner implies the
# services it depends on came up too.
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=40s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/tolokaforge-standalone-entrypoint"]
CMD ["appliance"]
