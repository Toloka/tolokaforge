# RAG Service
FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY tolokaforge/env/rag_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY tolokaforge/env/rag_service/ .

# Create data directory
RUN mkdir -p /env/rag

# Expose port
EXPOSE 8001

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
