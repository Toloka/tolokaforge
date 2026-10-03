# All-in-one (single-image, multi-service) tolokaforge image.
#
# Runs the whole runner stack — db-service (8000), rag-service (8001),
# mock-web (8080), and the runner gRPC service (50051) — inside ONE container,
# supervised by supervisord. The five per-component images published from this
# repo stay the distributed/scale-out shape; this image is the "just runs
# anywhere Docker does" shape. See docs/adr/0053-all-in-one-image.md.
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
# The INSTALL_BROWSER / INSTALL_DOCKER_CLI build args bake the two common
# heavy add-ons at build time (same opt-in pattern as runner.Dockerfile).
#
# Three stages: wheel-builder (base + sibling wheels from source, like
# grader.Dockerfile) -> builder (one shared /opt/venv with extras + the rag
# search deps + the baked embedding model) -> runtime (copy the venv, wire the
# four services under supervisord).

ARG PYTHON_VERSION=3.12
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# ---------------------------------------------------------------------------
# wheel-builder — build the base wheel + sibling wheels from source
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"

COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/
COPY tolokaforge_models/ /src/tolokaforge_models/
COPY tolokaforge_coding_harnesses/ /src/tolokaforge_coding_harnesses/

# Ship the whole tolokaforge distribution, models + coding-harnesses included
# (same as grader.Dockerfile — the all-in-one image needs the orchestrator,
# runner, env services, adapters, and dx front-end all present).
RUN python -m hatchling build --target wheel && \
    cd /src/tolokaforge_models && python -m hatchling build && \
    cd /src/tolokaforge_coding_harnesses && python -m hatchling build

# ---------------------------------------------------------------------------
# builder — one shared venv for all four services
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG EMBEDDING_MODEL
# Lean-base extras. Override with --build-arg TOLOKAFORGE_EXTRAS=... to add
# office/adapters/otel, or layer them post-build (pip is kept in runtime).
ARG TOLOKAFORGE_EXTRAS="dx,server,rag,runner"

# Build-only toolchain — never reaches the runtime stage (which copies only
# the venv). Covers any sdist-only transitive dep that needs a compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY --from=wheel-builder /src/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_coding_harnesses/dist/*.whl /tmp/wheels/

# Sibling wheels first so their versions resolve before the base wheel that
# depends on them (matching runner/grader install order).
RUN /opt/venv/bin/pip install --no-cache-dir \
        /tmp/wheels/tolokaforge_models-*.whl \
        /tmp/wheels/tolokaforge_coding_harnesses-*.whl

# CPU-only torch BEFORE sentence-transformers so the multi-GB CUDA build is
# never pulled. This is the single biggest size win for the all-in-one image —
# the rag search deps dominate it otherwise. Pinned to the CPU index.
RUN /opt/venv/bin/pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu torch

# Base wheel with the lean extras, then the rag-service's own search deps
# (sentence-transformers + numpy; rank-bm25 arrives via the [rag] extra), then
# the process supervisor. The rag requirements.txt is the SSOT for the search
# stack — install it rather than re-listing deps here.
COPY tolokaforge/env/rag_service/requirements.txt /tmp/rag-requirements.txt
RUN BASE_WHL="$(ls /tmp/wheels/tolokaforge-*.whl)" && \
    /opt/venv/bin/pip install --no-cache-dir "${BASE_WHL}[${TOLOKAFORGE_EXTRAS}]" && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/rag-requirements.txt && \
    /opt/venv/bin/pip install --no-cache-dir "supervisor>=4.2,<5" && \
    rm -rf /tmp/wheels /tmp/rag-requirements.txt

# Bake the embedding model into the venv image so the rag-service stands up
# with no HuggingFace round-trip. Must run with network on (no HF_HUB_OFFLINE
# yet) and after sentence-transformers is installed; the runtime stage sets
# HF_HUB_OFFLINE=1 so the baked weights are the ones served.
ENV HF_HOME=/opt/hf-cache
RUN /opt/venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

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

# Dedicated non-root service user (AGENTS.md). supervisord runs as pid 1 (root,
# so the entrypoint can add the loopback host aliases) and launches every
# service program as this user — the workloads never run as root.
RUN useradd --create-home --uid 10001 runner

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf-cache /opt/hf-cache

# Each env service is a FastAPI app named `app` with no package __init__, run
# today as `uvicorn app:app` with the service dir as CWD. Give each its own
# app-dir so the three `app` modules don't collide in one interpreter path.
COPY tolokaforge/env/json_db_service/app.py /srv/db/app.py
COPY tolokaforge/env/rag_service/app.py /srv/rag/app.py
COPY tolokaforge/env/mock_web_service/app.py /srv/web/app.py

COPY tolokaforge/docker/dockerfiles/standalone/supervisord.conf /etc/supervisor/standalone.conf
COPY tolokaforge/docker/dockerfiles/standalone/entrypoint.sh /usr/local/bin/tolokaforge-standalone-entrypoint

RUN chmod +x /usr/local/bin/tolokaforge-standalone-entrypoint && \
    mkdir -p /env/rag /work /var/log/tolokaforge && \
    chown -R runner:runner /srv /env /work /var/log/tolokaforge /opt/hf-cache

# Opt-in heavy add-ons, same pattern as runner.Dockerfile. pip is retained
# below (not stripped) so these can also be layered post-build by a derived
# image — the PyPI-extras analog.
ARG INSTALL_BROWSER=false
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
    /opt/venv/bin/pip install --no-cache-dir 'playwright>=1.40.0' 'pillow>=12.1.0' && \
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

EXPOSE 8000 8001 8080 50051

# Healthy once the runner's gRPC channel is ready (ADR-0024 surface). The
# runner starts last (supervisord priority), so a ready runner implies the
# services it depends on came up too.
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=40s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/tolokaforge-standalone-entrypoint"]
CMD ["appliance"]
