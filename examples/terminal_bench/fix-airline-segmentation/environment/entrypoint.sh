#!/bin/bash
set -e

# Start PostgreSQL
service postgresql start

# Wait for PostgreSQL to be ready
for i in $(seq 1 30); do
    su - postgres -c "pg_isready" >/dev/null 2>&1 && break
    sleep 1
done

exec "$@"
