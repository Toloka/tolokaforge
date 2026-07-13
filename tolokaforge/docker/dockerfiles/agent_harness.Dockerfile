FROM node:22-bookworm-slim

ARG HARNESS_TYPE=claude-code
ARG HARNESS_VERSION=2.1.203

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git procps python3 ripgrep \
    && case "${HARNESS_TYPE}" in \
         claude-code) npm install --global "@anthropic-ai/claude-code@${HARNESS_VERSION}" && claude --version ;; \
         codex) npm install --global "@openai/codex@${HARNESS_VERSION}" && codex --version ;; \
         acp) python3 -m venv /opt/acp && /opt/acp/bin/pip install --no-cache-dir agent-client-protocol ;; \
         *) echo "Unsupported harness type: ${HARNESS_TYPE}" >&2; exit 2 ;; \
       esac \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /work

COPY tolokaforge/harnesses/acp_runner.py /opt/tolokaforge-acp-runner.py

CMD ["sleep", "infinity"]
