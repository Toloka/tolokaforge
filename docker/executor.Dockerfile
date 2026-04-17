# Executor container - has access to env network
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Install Playwright for browser tool
RUN pip install playwright && \
    playwright install --with-deps chromium

# Copy application code
COPY tolokaforge/ ./tolokaforge/

# Create work directory for bash tool
RUN mkdir -p /work && chmod 755 /work

CMD ["python", "-m", "tolokaforge.executor"]
