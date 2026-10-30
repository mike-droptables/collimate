# Image for the agent generator's `cline` backend (the Cline CLI).
#
# `npm i -g cline` pulls the platform binary as an optional dependency and
# the postinstall hard-links it to bin/.cline inside the npm tree — nothing
# lands outside the image layer, so the plain global install is enough.
#
# PID 1 is `sleep infinity`; engine-driven turns attach `cline --acp` via
# docker exec (the attached-stdio ACP transport). The ACP arm reads its
# provider/model/key from CLINE_PROVIDER / CLINE_MODEL / CLINE_API_KEY env
# (never `-P`, which the ACP mode ignores) — the spawn profile sets them, so
# the run never touches Cline's default provider (Cline's own gateway, which
# would demand a Cline account sign-in).
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`.
ARG CLINE_VERSION=3.0.73
RUN npm install -g cline@${CLINE_VERSION}

# The owned native MCP settings use this direct stdio-to-HTTP client. Cline
# ignores ACP mcpServers (verified at 3.0.65); user-declared servers remain
# unsupported.
# The direct command avoids Cline's npx normalization to its OAuth HTTP path.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace so the
# CLI can write there. The per-cell harness home is bind-mounted over
# /home/coder/.cline (settings + session store).
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace \
    && chown -R coder:coder /home/coder /workspace

USER coder
WORKDIR /workspace

# Stay alive so the generator can attach ACP turns via exec.
CMD ["sleep", "infinity"]
