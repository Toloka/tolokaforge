# Mock Web Service
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Install dependencies under image-constraints.txt, the uv.lock export, and check
# the result against it (see the runner.Dockerfile header). The image keeps
# PIP_CONSTRAINT and PIP_BUILD_CONSTRAINT pointing at it, so a derived image's
# `pip install` pins every package the lock lists.
COPY tolokaforge/docker/image-constraints.txt tolokaforge/docker/dockerfiles/locked_install_check.py /opt/tolokaforge/
ENV PIP_CONSTRAINT=/opt/tolokaforge/image-constraints.txt \
    PIP_BUILD_CONSTRAINT=/opt/tolokaforge/image-constraints.txt
COPY tolokaforge/env/mock_web_service/requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: --no-binary sly -r requirements.txt \
    && python /opt/tolokaforge/locked_install_check.py \
        --constraints /opt/tolokaforge/image-constraints.txt --exempt pip

# Copy service code
COPY tolokaforge/env/mock_web_service/ .

# Expose port
EXPOSE 8080

# python-urllib probe avoids a curl dependency on the slim base.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
