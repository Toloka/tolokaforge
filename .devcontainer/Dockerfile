FROM ubuntu:jammy

SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-o", "nounset", "-o", "xtrace", "-c"]

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

RUN apt-get update -y && apt-get install -y \
    curl \
    wget \
    jq \
    git \
    git-lfs \
    rsync \
    apt-transport-https \
    ca-certificates \
    gnupg \
    python3 \
    python3-pip \
    python3-dev \
    python3-setuptools \
    python3-wheel \
    python3-venv \
    build-essential \
    libdbus-1-dev libdbus-glib-1-dev \
    # Dependencies for building Python extensions
    tk-dev liblzma-dev libssl-dev build-essential zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    # Dependencies for Playwright
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    sudo \
    locales \
    tzdata \
    zsh \
    # https://github.com/BurntSushi/ripgrep
    ripgrep \
    # https://github.com/sharkdp/bat
    bat \
    && rm -rf /var/lib/apt/lists/*

# Install Github cli: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
RUN mkdir -p -m 755 /etc/apt/keyrings \
        && out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        && cat $out | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& apt update \
	&& apt install gh -y \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS (includes npm and npx) — needed for MCP servers (e.g. @perplexity-ai/mcp-server)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install 'ruff==0.14.4' 'black==25.11.0'

# Set locale
RUN locale-gen en_US.UTF-8

# Add terminal colors
RUN echo "PS1='\e[92m\u\e[0m@\e[94m\h\e[0m:\e[35m\w\e[0m# '" >> /root/.bashrc

# Setup non-root user
ARG USER
ARG USER_UID=1000
ARG USER_GID=$USER_UID

COPY setup_user.sh setup_terminal.sh .zshrc /devcontainer_bootstrap/
RUN /devcontainer_bootstrap/setup_user.sh

USER $USER

RUN /devcontainer_bootstrap/setup_terminal.sh

# Pin uv to match the version CI uses (.github/workflows/ci.yml) and stay within
# [tool.uv] required-version in pyproject.toml. An unpinned install grabs "latest",
# which drifts outside that range and makes uv refuse to run in the container.
# Note: ~/.local is overlaid by the ${localWorkspaceFolderBasename}-local named
# volume at runtime, so bumping this pin requires removing that volume to take
# effect on an existing container (a fresh rebuild alone won't).
RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh

# Install Claude Code CLI as the non-root user via the native installer.
# Installing per-user (to ~/.local/bin, already on PATH) instead of a root/global
# npm install keeps the binary writable by the user, so background auto-updates and
# `claude update` work. A root-owned global install cannot self-upgrade.
RUN curl -fsSL https://claude.ai/install.sh | bash
