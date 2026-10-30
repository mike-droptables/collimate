# Image for the agent generator's `vibe` backend: Mistral Vibe (`vibe` TTY +
# `vibe-acp`, its ACP server) in its own container.
#
# Vibe 3.x ships as one Python wheel: the `vibe` launcher (which execs the
# Rust TUI bundled in the wheel), `vibe-acp` and the `vibe-app-server` the TUI
# talks to. It is installed as root into its own venv under /opt — root-owned
# on purpose, so neither Vibe's updater nor the agent's shell can replace the
# build mid-run — on the base's Debian Python (Vibe needs >= 3.12).
#
# PID 1 is `sleep infinity`; engine-driven turns attach `vibe-acp` over docker
# exec (the attached-stdio ACP transport) and the terminal surface runs `vibe`.
# VIBE_HOME is the per-cell harness home, bind-mounted at /home/coder/.vibe;
# the commands policy rides the VIBE_* environment, which outranks every
# config file there.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`. The build ends by asking both entry points
# for their version, with HOME redirected so no root-owned ~/.vibe is left
# behind.
ARG VIBE_VERSION=3.0.2
RUN set -eux; \
    /usr/bin/python3 -m venv /opt/vibe; \
    /opt/vibe/bin/pip install --no-cache-dir "mistral-vibe==${VIBE_VERSION}"; \
    for bin in vibe vibe-acp vibe-app-server; do ln -s "/opt/vibe/bin/${bin}" "/usr/local/bin/${bin}"; done; \
    HOME=/tmp/vibe-probe VIBE_HOME=/tmp/vibe-probe/.vibe vibe --version | grep -F "${VIBE_VERSION}"; \
    HOME=/tmp/vibe-probe VIBE_HOME=/tmp/vibe-probe/.vibe vibe-acp --version | grep -F "${VIBE_VERSION}"; \
    rm -rf /tmp/vibe-probe; \
    /opt/vibe/bin/python -m compileall -q /opt/vibe/lib >/dev/null || true

# The stdio↔HTTP MCP proxy (ACP carrier B): vibe-acp advertises no HTTP MCP
# capability, so the collimate tool bridge reaches it through this.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace and the
# per-cell harness home.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace /home/coder/.vibe \
    && chown -R coder:coder /home/coder /workspace

USER coder
WORKDIR /workspace

CMD ["sleep", "infinity"]
