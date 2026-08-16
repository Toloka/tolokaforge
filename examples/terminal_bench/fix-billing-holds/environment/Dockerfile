FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-15 \
        postgresql-client-15 \
        curl \
        procps \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        fastapi \
        uvicorn[standard] \
        sqlalchemy \
        psycopg2-binary \
        requests \
        pytest

COPY app/ /app/

# Initialize PostgreSQL with schema and seed data during image build
COPY init.sql /tmp/init.sql
RUN service postgresql start && \
    su - postgres -c "psql -c \"CREATE USER billing WITH PASSWORD 'billing';\"" && \
    su - postgres -c "psql -c \"CREATE DATABASE billing_db OWNER billing;\"" && \
    su - postgres -c "psql -d billing_db -f /tmp/init.sql" && \
    su - postgres -c "psql -d billing_db -c 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO billing;'" && \
    su - postgres -c "psql -d billing_db -c 'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO billing;'" && \
    service postgresql stop && \
    rm /tmp/init.sql

RUN mkdir -p /logs/verifier

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
COPY start-billing-service /usr/local/bin/start-billing-service
RUN chmod +x /entrypoint.sh /usr/local/bin/start-billing-service && \
    mkdir -p /run /logs

WORKDIR /app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
