# Orchestrator container - has access to env network and coordinates all services
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Copy application code
COPY tolokaforge/ ./tolokaforge/

# Create output directory
RUN mkdir -p /app/output && chmod 755 /app/output

# Default command (can be overridden in docker-compose)
CMD ["python", "-m", "tolokaforge.cli.main", "run", "--config", "/app/run_config.yaml"]
