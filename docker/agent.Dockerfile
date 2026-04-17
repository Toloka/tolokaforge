# Agent container - isolated, no network
FROM python:3.10-slim

# Security: Run as non-root
RUN useradd -m -u 1000 agent
USER agent

WORKDIR /work

# Copy application code and dependencies
COPY --chown=agent:agent pyproject.toml /app/
COPY --chown=agent:agent tolokaforge/ /app/tolokaforge/
RUN pip install --user --no-cache-dir /app/

# Agent has no access to environment secrets
# Files are read-only

CMD ["python", "-m", "tolokaforge.agent"]
