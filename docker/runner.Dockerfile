# Runner Container - Tool execution + grading
# 
# This container runs the Runner gRPC service that handles:
# - Trial registration with TaskDescription
# - Tool execution (MCP async, MCP server styles)
# - Trial grading via golden path comparison
# - State management via DB Service
#
# See docs/GRPC_PROTOCOL.md for the full protocol specification.

FROM python:3.11-slim

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

# Copy pyproject.toml and install dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e ".[docker]"

# Playwright for browser tool (opt-in via build arg)
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    pip install playwright && playwright install --with-deps chromium; \
    fi

# Copy tolokaforge package
COPY tolokaforge/ ./tolokaforge/

# Note: mcp_core and mcp_tools_library are NOT baked into the image.
# FrozenMcpCoreAdapter and NativeAdapter transmit them via tool_artifacts
# (base64-encoded files in TaskDescription) which the Runner extracts at
# runtime into a temp directory and adds to sys.path automatically.

# Install dependencies needed by mcp_core and mcp_tools_library
# These are the runtime dependencies that don't require private packages
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

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV DB_SERVICE_URL=http://db-service:8000
ENV RAG_SERVICE_URL=http://rag-service:8001

# gRPC port
EXPOSE 50051

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=2)" || exit 1

# Run the Runner service
CMD ["python", "-m", "tolokaforge.runner"]
