# Image for the agent generator's `hermes` backend: NousResearch's Hermes
# Agent (`hermes` TTY and `hermes acp`, its ACP server) in its own container.
#
# Hermes installs from its source checkout with its own installer, which
# provisions the Python, Node and ripgrep it runs on under its install home
# (the system ones are not used). It is installed as root into /opt/hermes and
# pinned to the release tag the run resolved; the checkout and its toolchain
# stay root-owned, so neither `hermes update` nor the agent's shell can
# replace the build mid-run. Only the dependency-environment state
# (/opt/hermes/installs, which Hermes takes a lock in) belongs to `coder`.
#
# PID 1 is `sleep infinity`; engine-driven turns attach `hermes acp` over
# docker exec, and the terminal surface runs `hermes`. HERMES_HOME is the
# per-cell harness home, bind-mounted at /home/coder/.hermes; the commands
# policy is Hermes's root-owned managed scope (/etc/hermes), which outranks
# every file there.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

# What the installer itself needs (git, curl and xz come with or after the
# base) plus libatomic1, which its Node toolchain links.
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`. The installer comes from the same tag it
# installs, and --commit pins that tag exactly. The optional Anthropic SDK is
# installed now, not lazily at first use (a run's container may not fetch).
# The build ends by asking the launcher for its version.
ARG HERMES_VERSION=0.22.5
ENV HERMES_RUNTIME_DIR=/opt/hermes/tools
RUN set -eux; \
    v="${HERMES_VERSION#v}"; \
    curl -fsSL -o /tmp/hermes-install.sh \
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/v${v}/scripts/install.sh"; \
    HOME=/root bash /tmp/hermes-install.sh --non-interactive --skip-browser --skip-computer-use \
        --dir /opt/hermes/hermes-agent --hermes-home /opt/hermes --commit "v${v}"; \
    rm -rf /root/.local /tmp/hermes-install.sh; \
    ln -s /opt/hermes/hermes-agent/.hermes/bin/hermes /usr/local/bin/hermes; \
    ln -s /opt/hermes/hermes-agent/.hermes/bin/hermes-acp /usr/local/bin/hermes-acp; \
    HOME=/root HERMES_HOME=/opt/hermes hermes pm install --extra anthropic; \
    git config --system --add safe.directory '*'; \
    HOME=/root HERMES_HOME=/opt/hermes hermes --version | grep -F "v${v}"; \
    rm -rf /opt/hermes/logs /opt/hermes/sessions /opt/hermes/state.db*

# The stdio↔HTTP MCP proxy (ACP carrier B): hermes advertises no HTTP MCP
# capability, so the collimate tool bridge reaches it through this.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace and the
# per-cell harness home. /etc/hermes is the managed scope's directory, written
# by the generator as root on every leg.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace /home/coder/.hermes /etc/hermes \
    && chown -R coder:coder /home/coder /workspace /opt/hermes/installs \
    && chmod 755 /etc/hermes

# Hermes treats a container as one: keep the agent's shell on the real HOME.
ENV HERMES_HOME=/home/coder/.hermes TERMINAL_HOME_MODE=real
USER coder
WORKDIR /workspace

CMD ["sleep", "infinity"]
