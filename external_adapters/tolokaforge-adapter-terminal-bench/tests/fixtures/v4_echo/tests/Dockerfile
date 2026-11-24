FROM python:3.13-slim
RUN pip install --no-cache-dir pytest==9.1.1
COPY . /tests/
# The verifier drops to an unprivileged user the way a hardened pack's does, so
# every exec grading makes into this container lands as that user unless the
# task's declared ``[verifier] user`` is carried through. /app is the artifact
# landing site and stays root-owned, so the staging that empties it and recreates
# its parent only succeeds as that user; the reports are sealed by test.sh.
RUN mkdir -p /app /logs/verifier \
    && useradd --create-home --uid 12000 verifier
USER verifier
