# Mock Web Service
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Install dependencies
COPY tolokaforge/env/mock_web_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY tolokaforge/env/mock_web_service/ .

# Expose port
EXPOSE 8080

# python-urllib probe avoids a curl dependency on the slim base.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
