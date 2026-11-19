FROM python:3.10-slim
RUN useradd --create-home agent && mkdir -p /app/workdir && chown -R agent /app
WORKDIR /app
CMD ["sleep", "infinity"]
