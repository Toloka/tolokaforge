# DB Service Container - State storage with trial isolation
#
# This container runs the DB Service HTTP API that provides:
# - Trial-scoped state management
# - Snapshot/restore for golden path execution
# - Stable state hash computation
# - JSONPath queries and SQL queries
#
# See docs/DB_SERVICE_API.md for the full API specification.

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY tolokaforge/env/json_db_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY tolokaforge/env/json_db_service/ .

# Environment variables
ENV PYTHONUNBUFFERED=1

# HTTP port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8000/health || exit 1

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
