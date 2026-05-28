FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-15 \
        postgresql-client-15 \
        curl \
        procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        pandas \
        numpy \
        scikit-learn \
        psycopg2-binary \
        sqlalchemy \
        pytest

# Copy pipeline code
COPY pipeline/ /app/

# Copy documentation
COPY docs/ /app/docs/

# Initialize PostgreSQL: create user, database, schema, and seed data
COPY init.sql /tmp/init.sql
COPY generate_data.py /tmp/generate_data.py

RUN service postgresql start && \
    su - postgres -c "psql -c \"CREATE USER airline WITH PASSWORD 'seg_pipeline_2024';\"" && \
    su - postgres -c "psql -c \"CREATE DATABASE airline_db OWNER airline;\"" && \
    su - postgres -c "psql -d airline_db -f /tmp/init.sql" && \
    su - postgres -c "psql -d airline_db -c 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO airline;'" && \
    su - postgres -c "psql -d airline_db -c 'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO airline;'" && \
    python /tmp/generate_data.py && \
    su - postgres -c "psql -d airline_db -f /tmp/seed_data.sql" && \
    cd /app && PG_HOST=localhost PG_PASSWORD=seg_pipeline_2024 python main.py && \
    service postgresql stop && \
    rm /tmp/init.sql /tmp/generate_data.py /tmp/seed_data.sql

RUN mkdir -p /logs/verifier /logs/pipeline

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
