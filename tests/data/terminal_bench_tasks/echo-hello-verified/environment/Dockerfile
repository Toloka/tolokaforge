FROM python:3.10-slim
# postgresql-client is the oracle's only dependency: solution/solve.sh seeds the
# greeting row the verifier's collect hook dumps and its test.sh greps for.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home agent && mkdir -p /app/workdir && chown -R agent /app
WORKDIR /app
CMD ["sleep", "infinity"]
