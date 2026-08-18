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

# Launch the billing service under supervisord so a model that later
# kills+relaunches uvicorn in the foreground (dropping `&`) does not
# leave the service dead at grade time. See supervisord.conf.
mkdir -p /logs
supervisord -c /etc/supervisor/conf.d/supervisord.conf
echo "Billing service supervised on port 8000"

exec "$@"
