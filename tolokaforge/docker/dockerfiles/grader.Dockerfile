# Grader Container — standalone grader service (ADR-0035, P4).
#
# Ships the tolokaforge grader service (see docs/GRADER_SERVICE.md) as an
# independently-deployable image alongside the runner / db-service /
# rag-service / mock-web images already published from this repo. The seam
# consumer is ``GraderRPCTrialGrader`` in ``tolokaforge.core.trial_grader``,
# registered under the ``grader_rpc`` plug-in name.
#
# The current build installs the full ``tolokaforge`` wheel and runs
# ``python -m tolokaforge.grader`` on top of it. A grader-only subset
# wheel (analogous to the runner subset — see ADR-0025 and
# ``scripts/hatch/hatch_runner_subset_builder.py``) is a follow-up: the
# image ships now with the pattern proven at the deployment layer; the
# closure-narrowing optimisation lands after.
#
# Preserves the tolokasoft1/tolokaforge-<component> image-name axis
# (ADR-0023). The container command surface is the ``python -m
# tolokaforge.grader`` entry, which reads ``--port`` (or the
# ``GRADER_SERVICE_PORT`` env var, default 50052).

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# wheel-builder — build the base wheel + models wheel from source
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS wheel-builder

WORKDIR /src

RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"

COPY pyproject.toml README.md LICENSE .python-version /src/
COPY scripts/hatch/ /src/scripts/hatch/
COPY tolokaforge/ /src/tolokaforge/
COPY tolokaforge_models/ /src/tolokaforge_models/

# Build the base wheel — the grader ships the whole tolokaforge distribution
# for now, tolokaforge_models included. Subset build target is a follow-up.
RUN python -m hatchling build --target wheel && \
    cd /src/tolokaforge_models && python -m hatchling build

# ---------------------------------------------------------------------------
# builder — install the wheel into /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY --from=wheel-builder /src/dist/*.whl /tmp/wheels/
COPY --from=wheel-builder /src/tolokaforge_models/dist/*.whl /tmp/wheels/

# Install the models wheel first so its version resolves before the base
# wheel that depends on it (matching the runner image install order).
RUN /opt/venv/bin/pip install --no-cache-dir /tmp/wheels/tolokaforge_models-*.whl && \
    /opt/venv/bin/pip install --no-cache-dir /tmp/wheels/tolokaforge-*.whl

# ---------------------------------------------------------------------------
# runtime — copy only the venv; strip pip/setuptools/wheel for image size
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* && \
    apt-get clean

COPY --from=builder /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GRADER_SERVICE_PORT=50052

# Strip the packaging toolchain; the runtime image is a service, not a
# builder. Matches runner.Dockerfile's finalising step for image parity.
RUN /opt/venv/bin/pip uninstall -y pip setuptools wheel

EXPOSE 50052

# The grader service reads --port / $GRADER_SERVICE_PORT; the default is
# preserved via the entry point's argparse fall-through.
CMD ["python", "-m", "tolokaforge.grader"]
