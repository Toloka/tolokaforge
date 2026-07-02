# JSON DB service
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Install dependencies
COPY tolokaforge/env/json_db_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY tolokaforge/env/json_db_service/ .

# Expose port
EXPOSE 8000

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
