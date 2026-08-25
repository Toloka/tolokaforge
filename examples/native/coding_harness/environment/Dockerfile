FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir pytest==8.3.3

WORKDIR /work

COPY app/factorial.py /work/factorial.py
COPY app/tests /work/tests

RUN mkdir -p /logs/verifier
