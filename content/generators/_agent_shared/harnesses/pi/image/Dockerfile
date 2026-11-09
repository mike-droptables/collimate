# Image for the agent generator's `pi` backend (the pi coding agent behind
# the `pi-acp` adapter).
#
# Two artifacts by design: `pi-acp` is the ACP stdio adapter and
# `@earendil-works/pi-coding-agent` is the agent it spawns (`pi --mode rpc`)
# — the adapter fails session/new with ENOENT without the latter on PATH.
#
# PID 1 is `sleep infinity`; engine-driven turns attach `pi-acp` via docker
# exec (the attached-stdio ACP transport), which spawns pi through the
# commands-grant wrapper below. Preparation writes private native provider
# credentials/model settings; ACP applies its selected model through native
# configuration. No provider key is added to the adapter environment.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# cli_container supplies the resolved or canonical fallback adapter version.
# The native coding-agent ARG retains its offline fallback; both artifacts
# otherwise resolve latest unless the runner pins them in `packages:`.
ARG PI_ACP_VERSION
ARG PI_CODING_AGENT_VERSION=1.4.0
RUN : "${PI_ACP_VERSION:?cli_container must supply PI_ACP_VERSION}" && npm install -g pi-acp@${PI_ACP_VERSION} \
    @earendil-works/pi-coding-agent@${PI_CODING_AGENT_VERSION}

# Existing declared ACP image plumbing remains pinned. pi-acp ignores ACP
# mcpServers (verified at 0.0.34), so cell_mcp remains false. Its own Collimate bridge is
# delivered by the immutable native extension and official client below;
# that route does not use this proxy or deliver user-declared servers.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# The commands-grant wrapper and gate (see collimate-pi): root-owned, so the
# agent running as coder can read and run them but never rewrite them. The
# profile points pi-acp at the wrapper (PI_ACP_PI_COMMAND) and the terminal
# leg runs it in place of `pi`.
ARG MCP_SDK_VERSION=1.32.2
RUN npm install --prefix /opt/collimate/pi --omit=dev --no-audit --no-fund @modelcontextprotocol/sdk@${MCP_SDK_VERSION}

# pi-acp drops the stderr of the pi it spawns; the wrapper joins pi's stderr
# to the adapter's own on the ACP leg (COLLIMATE_PI_STDERR), so no source
# patch of the adapter is needed and an adapter bump cannot break the build.
COPY collimate-pi gate.ts mcp.js /opt/collimate/pi/
RUN chown -R root:root /opt/collimate \
    && chmod 755 /opt/collimate /opt/collimate/pi /opt/collimate/pi/collimate-pi \
    && chmod 644 /opt/collimate/pi/gate.ts /opt/collimate/pi/mcp.js

# uid 1000 must match the host that owns the bind-mounted /workspace so the
# agent can write there. The per-cell harness home is bind-mounted over
# /home/coder/.pi (pi's session store + the adapter's session map).
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace \
    && chown -R coder:coder /home/coder /workspace

USER coder
WORKDIR /workspace

# Stay alive so the generator can seed settings + attach ACP turns via exec.
CMD ["sleep", "infinity"]
