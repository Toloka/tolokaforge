# DB Service Container - State storage with trial isolation
#
# This container runs the DB Service HTTP API that provides:
# - Trial-scoped state management
# - Snapshot/restore for golden path execution
# - Stable state hash computation
# - JSONPath queries and SQL queries
#
# See docs/DB_SERVICE_API.md for the full API specification.

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Install dependencies under image-constraints.txt, the uv.lock export, and check
# the result against it (see the runner.Dockerfile header). The image keeps
# PIP_CONSTRAINT and PIP_BUILD_CONSTRAINT pointing at it, so a derived image's
# `pip install` pins every package the lock lists.
COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt
COPY tolokaforge/env/json_db_service/requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: --no-binary sly -r requirements.txt \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip

# Copy service code
COPY tolokaforge/env/json_db_service/ .

# Environment variables
ENV PYTHONUNBUFFERED=1

# HTTP port
EXPOSE 8000

# Health check — stdlib urllib probe (no curl layer), matching mock_web/rag.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
