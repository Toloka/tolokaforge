#!/bin/bash
# Start PostgreSQL
service postgresql start

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to start..."
for i in $(seq 1 15); do
    pg_isready -U billing -d billing_db -h localhost >/dev/null 2>&1 && break
    sleep 1
done
echo "PostgreSQL ready!"

# Start the billing service in the background
cd /app && uvicorn main:app --host 0.0.0.0 --port 8000 &
echo "Billing service started on port 8000"

exec "$@"
