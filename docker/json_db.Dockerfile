# JSON DB service
FROM python:3.10-slim

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
