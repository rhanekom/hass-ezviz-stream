# Devcontainer image for hass-ezviz-stream.
#
# This holds GLOBAL, machine-wide setup only (system packages + standalone tool
# binaries). It changes rarely, so its layers stay cached across the far more
# frequent `scripts/setup` runs (postCreateCommand), which handle user-specific
# and project-specific setup. Keep that split: global -> here, per-user -> setup.

FROM mcr.microsoft.com/devcontainers/python:3.13

# Pinned versions for directly-downloaded tools.
# Keep these in sync with the matching rev in .pre-commit-config.yaml.
ARG GITLEAKS_VERSION=8.30.1
ARG ACTIONLINT_VERSION=1.7.12

# System packages:
#   ffmpeg / libturbojpeg0 - media decode + JPEG for the stream pipeline
#   libpcap-dev            - packet capture for protocol reverse-engineering
#   shellcheck             - shell linting (editor + manual use)
RUN apt-get update \
    && apt-get -y dist-upgrade \
    && apt-get install -y \
        ffmpeg \
        libturbojpeg0 \
        libpcap-dev \
        shellcheck \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh) from the official apt repo.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# gitleaks - secret scanning (system binary for the gitleaks-system pre-commit hook).
RUN curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks

# actionlint - GitHub Actions linting (system binary for the actionlint-system hook).
RUN curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin actionlint

# uv - Python dependency manager (global; manages pyproject.toml / uv.lock).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Upgrade pip (machine-wide).
RUN python3 -m pip install --upgrade pip
