FROM python:3.13-slim
RUN pip install --no-cache-dir pytest==9.1.1
COPY . /tests/
# The verifier drops to an unprivileged user the way a hardened pack's does, so
# every exec grading makes into this container lands as that user unless the
# task's declared ``[verifier] user`` is carried through. /app is the artifact
# landing site and holds a stale root-owned answer in a root-owned parent, so
# the staging that empties it and recreates that parent only succeeds as the
# declared user; the reports are sealed by test.sh.
RUN mkdir -p /app /logs/verifier \
    && useradd --create-home --uid 12000 verifier \
    && echo 99 > /app/answer.txt
USER verifier
