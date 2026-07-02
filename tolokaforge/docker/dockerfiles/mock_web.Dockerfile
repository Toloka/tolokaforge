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

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
