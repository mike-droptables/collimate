# Image for the agent generator's `copilot` backend: GitHub Copilot CLI
# (`copilot` TTY and `copilot --acp`, its ACP server) in its own container.
#
# The npm package is a thin loader for a native binary carried by its
# platform optional dependency (@github/copilot-linux-x64 / -arm64), so the
# install keeps optional dependencies. Root-owned under the global npm prefix:
# neither Copilot's own updater (also switched off by COPILOT_AUTO_UPDATE at
# run time) nor the agent's shell can replace the build mid-run.
#
# PID 1 is `sleep infinity`; engine-driven turns attach `copilot --acp` over
# docker exec (the attached-stdio ACP transport) and the terminal surface runs
# `copilot`. COPILOT_HOME is the per-cell harness home, bind-mounted at
# /home/coder/.copilot; the login is a GitHub token in COPILOT_GITHUB_TOKEN,
# never a keychain (a container has none).
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`. The build ends by asking the CLI for its
# version, with HOME redirected so no root-owned ~/.copilot is left behind.
ARG COPILOT_VERSION=1.0.104
RUN set -eux; \
    npm install -g "@github/copilot@${COPILOT_VERSION}"; \
    HOME=/tmp/copilot-probe COPILOT_AUTO_UPDATE=false copilot --version | grep -F "${COPILOT_VERSION}"; \
    rm -rf /tmp/copilot-probe

# Managed settings: the device-level policy file Copilot reads from
# /etc/github-copilot/managed-settings.json (root-owned, so neither the agent
# nor its shell can change it). strictPluginOnlyCustomization locks the
# surfaces by which a file the agent can write (the workspace, $COPILOT_HOME)
# names a command Copilot then runs outside the commands gate: hooks
# (.github/hooks/*.json and hooks in settings files; `copilot --acp` loads them
# whether or not the folder is trusted, and `disableAllHooks` is not honoured
# in 1.0.103), custom agents (.github/agents, which carry MCP servers) and
# skills (.github/skills, whose allowed-tools pre-approve tools). The `mcp`
# surface stays open: locking it also drops the run's own tool bridge, which
# arrives as an ACP mcpServers entry; workspace MCP files load only from a
# trusted folder, and Collimate trusts none.
RUN mkdir -p /etc/github-copilot \
    && printf '%s\n' '{"strictPluginOnlyCustomization": ["skills", "agents", "hooks"]}' > /etc/github-copilot/managed-settings.json \
    && chmod 0644 /etc/github-copilot/managed-settings.json

# The stdio↔HTTP MCP proxy (ACP carrier B), for a harness advertising no HTTP
# MCP capability.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace and the
# per-cell harness home.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace /home/coder/.copilot \
    && chown -R coder:coder /home/coder /workspace

ENV COPILOT_AUTO_UPDATE=false
USER coder
WORKDIR /workspace

CMD ["sleep", "infinity"]
