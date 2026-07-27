# RAG Service - Hybrid BM25 + FAISS search
#
# Per-trial document indexing and search via FastAPI.
# See tolokaforge/env/rag_service/app.py for the API surface.

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Service-specific runtime deps (FAISS, BM25, sentence-transformers).
# Layered before the tolokaforge package install so changes there don't
# bust this layer's pip cache.
COPY tolokaforge/env/rag_service/requirements.txt ./service-requirements.txt
RUN pip install --no-cache-dir -r service-requirements.txt

# Install the tolokaforge package from a pre-built wheel so service code
# can ``import tolokaforge.secrets`` for the global log redactor.
# The wheel is placed in the build context by the host-side wheel resolver;
# WHEEL_FILENAME has no default so a missing --build-arg fails loudly at
# this layer rather than silently `COPY`ing the wrong filename later.
ARG WHEEL_FILENAME
COPY ${WHEEL_FILENAME} /tmp/
RUN pip install --no-cache-dir "/tmp/${WHEEL_FILENAME}" \
    && rm -f "/tmp/${WHEEL_FILENAME}"

# Copy the service entrypoint last (rebuild only on app.py changes).
# Lands at /app/app.py so ``uvicorn app:app`` continues to resolve it.
COPY tolokaforge/env/rag_service/app.py ./app.py

# Per-trial corpus storage
RUN mkdir -p /env/rag

ENV PYTHONUNBUFFERED=1

EXPOSE 8001

# python-urllib probe avoids a curl dependency on the slim base.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
