# Image for the agent generator's `opencode` mode.
#
# Lands a non-root `coder` user (uid 1000, matching the host that owns the
# bind-mounted /workspace) with the opencode CLI installed. PID 1 is
# `sleep infinity`; the generator seeds opencode's config with the picked model
# and then launches `opencode web` in the background, publishing its port so the
# cell can iframe opencode's own UI through the .website renderer.
#
# Built on the shared collimate-linux-small base (node 24/npm + git + curl +
# ca-certificates), so this image only layers on opencode itself. _ensure_image
# builds that base first from the _env_shared/base context when it isn't
# already present.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`.
ARG OPENCODE_VERSION=1.19.4
RUN npm install -g opencode-ai@${OPENCODE_VERSION}

# The stdio↔HTTP MCP proxy (ACP carrier B) — the collimate tool bridge stays
# reachable even when the harness advertises no HTTP MCP capability.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace so opencode
# can write there. The base has no uid-1000 user, so create `coder` at 1000.
#
# opencode loads plugins, custom tools and command hooks from its global config
# dir and ~/.opencode and runs them with no permission check, so the agent
# must not be able to write either: both are root-owned, inside a home and a
# ~/.config that are root-owned and sticky — coder still creates (and owns)
# its own files there, but cannot rename or replace the two directories.
# Collimate writes the config as root.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /home/coder/.config/opencode /home/coder/.opencode \
        /home/coder/.local/share/opencode /workspace \
    && chown -R coder:coder /home/coder /workspace \
    && chown root:root /home/coder /home/coder/.config \
        /home/coder/.config/opencode /home/coder/.opencode \
    && chmod 1777 /home/coder /home/coder/.config \
    && chmod 755 /home/coder/.config/opencode /home/coder/.opencode

USER coder
WORKDIR /workspace

# Stay alive so the generator can seed config + launch `opencode web` via exec.
CMD ["sleep", "infinity"]
