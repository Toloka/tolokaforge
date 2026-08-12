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

# Bake the embedding model into the image by performing the load the service
# performs, so the weights the runtime resolves are the ones tested here.
#
# This layer must stay above every layer carrying tolokaforge source: app.py
# ships inside the wheel installed below, so a bake placed after that COPY
# re-downloads 88MB on every edit to any tolokaforge file. Its cache key here
# is {base image, service-requirements.txt, EMBEDDING_MODEL, this command}.
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ENV HF_HOME=/opt/hf-cache
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

# Offline only after the bake — set before it, the bake itself is refused and
# the build fails. The runtime model name comes from the same ARG the bake
# used, so the model the service loads is by construction the baked one.
ENV HF_HUB_OFFLINE=1
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}

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
# The service loads the baked embedding model at startup with no network in the
# path — measured 3.5s from container start to serving, of which the load is
# 0.1s — and the grace covers that with headroom. Probe failures during
# start-period don't count toward retries, so the container still flips to
# healthy the instant /health returns 200.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
