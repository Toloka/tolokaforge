# Runner Container - Tool execution + grading
#
# This container runs the Runner gRPC service that handles:
# - Trial registration with TaskDescription
# - Tool execution (MCP server styles, terminal-bench)
# - Trial grading via golden path comparison
# - State management via DB Service
#
# tolokaforge is installed from a pre-built wheel placed into the build
# context by the host-side wheel resolver (tolokaforge.docker.wheel_resolver).
# The container never clones a repo or reaches PyPI — the wheel is local.

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose plugin (for terminal-bench tasks).
# No Docker daemon — uses host daemon via mounted /var/run/docker.sock.
RUN install -m 0755 -d /etc/apt/keyrings \
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

WORKDIR /app

# Install tolokaforge from the wheel the host resolver placed in the context.
# WHEEL_FILENAME is passed as a build arg by the wheel resolver so we don't
# rely on shell glob expansion inside Docker. No default — a missing
# --build-arg fails loudly at this layer rather than silently `COPY`ing the
# wrong filename later.
ARG WHEEL_FILENAME
COPY ${WHEEL_FILENAME} /tmp/
RUN pip install --no-cache-dir "/tmp/${WHEEL_FILENAME}[docker]" \
    && rm -f "/tmp/${WHEEL_FILENAME}"

# Playwright for browser tool (opt-in via build arg)
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    pip install playwright && playwright install --with-deps chromium; \
    fi

# Domain code is delivered at runtime via TaskDescription.tool_artifacts —
# adapters bundle the env/domain tree there, the runner extracts it under a
# per-trial tempdir and prepends it to sys.path. The image stays domain-agnostic.

# Install runtime dependencies required by extracted tool artifacts
RUN pip install --no-cache-dir \
    odata-query>=0.9.0 \
    sqlalchemy>=2.0.0 \
    asyncpg>=0.29.0 \
    psycopg2-binary>=2.9.0 \
    alembic>=1.13.0 \
    python-jose>=3.3.0 \
    typesense>=0.21.0 \
    starlette>=0.27.0 \
    mcp>=0.1.0 \
    fastapi>=0.108.0 \
    uvicorn>=0.25.0

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
ENV PYTHONUNBUFFERED=1

# gRPC port
EXPOSE 50051

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=2)" || exit 1

# Run the Runner service
CMD ["python", "-m", "tolokaforge.runner"]
