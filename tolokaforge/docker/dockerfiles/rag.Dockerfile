# RAG Service - Hybrid BM25 + FAISS search
#
# Per-trial document indexing and search via FastAPI.
# See tolokaforge/env/rag_service/app.py for the API surface.
#
# Embeddings run on CPU torch from the PyTorch CPU wheel index; the image
# carries no CUDA libraries. Every install runs under image-constraints.txt, the
# uv.lock export, and is checked against it (see the runner.Dockerfile header);
# the shipped image keeps PIP_CONSTRAINT and PIP_BUILD_CONSTRAINT pointing at it,
# so a derived image's `pip install` pins every package the lock lists.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# sibling-wheel-builder — build the workspace-sibling wheels the base wheel
# depends on (tolokaforge_models, tolokaforge_coding_harnesses). Kept as a
# separate stage so the sibling build cache only busts on sibling source
# edits — the model bake below stays warm across unrelated tolokaforge
# changes.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS sibling-wheel-builder

WORKDIR /src

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

RUN pip install --no-cache-dir --only-binary :all: --no-binary sly "hatchling>=1.24,<2.0" \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip

COPY tolokaforge_models/ /src/tolokaforge_models/
COPY tolokaforge_coding_harnesses/ /src/tolokaforge_coding_harnesses/

RUN cd /src/tolokaforge_models && python -m hatchling build && \
    cd /src/tolokaforge_coding_harnesses && python -m hatchling build

FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt

# Service-specific runtime deps (FAISS, BM25, sentence-transformers).
# Layered before the tolokaforge package install so changes there don't
# bust this layer's pip cache. CPU torch goes first, alone and without its
# dependencies, from the CPU index at the locked `+cpu` version; the PyPI install
# after it resolves torch's dependencies under the lock.
COPY tolokaforge/env/rag_service/requirements.txt ./service-requirements.txt
RUN pip install --no-cache-dir --only-binary :all: --no-binary sly \
        --no-deps --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir --only-binary :all: --no-binary sly -r service-requirements.txt \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip \
    && pip check \
    && python -c "import torch; assert torch.__version__.endswith('+cpu'), torch.__version__"

# Bake the embedding model into the image by performing the load the service
# performs, so the weights the runtime resolves are the ones tested here.
#
# This layer must stay above every layer carrying tolokaforge source: app.py
# ships inside the wheel installed below, so a bake placed after that COPY
# re-downloads 88MB on every edit to any tolokaforge file. Its cache key here
# is {base image, image-constraints.txt, locked_install_check.py,
# service-requirements.txt, EMBEDDING_MODEL, this command}, so every uv.lock
# change re-bakes it.
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
COPY --from=sibling-wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/
COPY --from=sibling-wheel-builder /src/tolokaforge_coding_harnesses/dist/*.whl /tmp/wheels/
# One install of the three local wheels, so the base wheel's
# ``tolokaforge-models`` requirement is met by the local wheel, never an index.
# The guard compares each first-party install with its wheel, so it runs before
# the wheels are deleted.
RUN pip install --no-cache-dir --only-binary :all: --no-binary sly \
        /tmp/wheels/tolokaforge_models-*.whl \
        /tmp/wheels/tolokaforge_coding_harnesses-*.whl \
        "/tmp/${WHEEL_FILENAME}" \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt \
        --first-party tolokaforge \
        --first-party tolokaforge-models \
        --first-party tolokaforge-coding-harnesses \
        --exempt pip \
    && rm -f "/tmp/${WHEEL_FILENAME}" /tmp/wheels/*.whl

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
