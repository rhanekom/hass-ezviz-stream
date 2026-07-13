# Devcontainer image for hass-ezviz-stream.
#
# This holds GLOBAL, machine-wide setup only (system packages + standalone tool
# binaries). It changes rarely, so its layers stay cached across the far more
# frequent `scripts/setup` runs (postCreateCommand), which handle user-specific
# and project-specific setup. Keep that split: global -> here, per-user -> setup.

FROM mcr.microsoft.com/devcontainers/python:3.14

# Pinned versions for directly-downloaded tools.
# Keep these in sync with the matching rev in .pre-commit-config.yaml.
ARG GITLEAKS_VERSION=8.30.1
ARG ACTIONLINT_VERSION=1.7.12

# Global, machine-wide setup in a single RUN layer (docker:S7031 — consecutive
# RUN instructions merged). This layer changes rarely, so it stays cached across
# the far more frequent scripts/setup runs.
#   ffmpeg / libturbojpeg0        - media decode + JPEG for the stream pipeline
#   libpcap-dev                   - packet capture for protocol reverse-engineering
#   shellcheck                    - shell linting (editor + manual use)
#   build-essential / procps / file - Homebrew (Linuxbrew) prerequisites
#   gh                            - GitHub CLI (from the official apt repo)
#   gitleaks                      - secret scanning (gitleaks-system pre-commit hook)
#   actionlint                    - GitHub Actions linting (actionlint-system hook)
#   uv                            - Python dependency manager (pyproject.toml / uv.lock)
#   pip                           - upgraded to latest (machine-wide)
RUN apt-get update \
    && apt-get -y dist-upgrade \
    && apt-get install -y \
        ffmpeg \
        libturbojpeg0 \
        libpcap-dev \
        shellcheck \
        build-essential \
        procps \
        file \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks \
    && curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin actionlint \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && python3 -m pip install --upgrade pip

# Homebrew (Linuxbrew) - package manager for user dev tools (e.g. lazygit,
# installed in scripts/setup). Must run as the non-root 'vscode' user because
# Homebrew refuses to run as root; switch back afterwards.
USER vscode
RUN NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
USER root
