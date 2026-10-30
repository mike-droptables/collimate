# Image for the agent generator's `codex` backend (OpenAI Codex CLI).
#
# Lands a non-root `coder` user (uid 1000, matching the host that owns the
# bind-mounted /workspace) with the `@openai/codex` CLI installed. PID 1 is
# `sleep infinity`; the generator seeds config.toml (a fresh CODEX_HOME) + keys
# and then runs `codex exec --json` (oneshot/chat) or attaches the `codex` TUI
# (terminal mode) via exec. The container is the exec boundary; the runner's
# commands grant picks codex's approval and sandbox mode inside it.
#
# Built on the shared collimate-linux-small base (node 24/npm + git + curl +
# ca-certificates), so this image only layers on codex itself. _ensure_image
# builds that base first from the _env_shared/base context when it isn't
# already present.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`.
#
# This CLI serves the TERMINAL surface (`codex` TUI / `codex resume`) only.
# The chat/oneshot legs never run it: codex-acp spawns `codex app-server`
# from the @openai/codex it bundles in its own node_modules (or $CODEX_PATH
# when set), never from PATH — verified against the adapter's dist/index.js
# (startCodexConnection). So the version here has no bearing on what the
# ACP wire speaks; keep it current for the TTY and for session-store parity
# with the adapter's bundled build.
ARG CODEX_VERSION=0.164.0
RUN npm install -g @openai/codex@${CODEX_VERSION}

# The ACP adapter (the engine's wire for oneshot/chat) and the stdio↔HTTP MCP
# proxy (carrier B: stdio is ACP's only mandatory MCP transport, so the
# collimate tool bridge must be reachable through it when a harness advertises
# no HTTP capability). The adapter bundles the codex it drives (2.2.2 →
# @openai/codex ^0.160.1), so the adapter's version, not CODEX_VERSION,
# decides the chat leg's codex.
# Canonical adapter fallback is supplied by cli_container.
ARG CODEX_ACP_VERSION
RUN : "${CODEX_ACP_VERSION:?cli_container must supply CODEX_ACP_VERSION}" && npm install -g @agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace (and the
# per-cell codex home) so codex can write there. The base has no uid-1000 user,
# so create `coder` at 1000. ~/.codex-collimate is CODEX_HOME under BOTH auths:
# the cell's own harness home, bind-mounted from the host. The user's real
# ~/.codex is never mounted; under auth=chatgpt the generator binds Collimate's
# own auth.json as a single file over $CODEX_HOME/auth.json once that login
# exists (see codex_backend.codex_workspace_links).
#
# /etc/codex is root's: the generator writes requirements.toml there (as root)
# on every leg — codex's hard lock on approval policy, reviewer and features,
# which nothing the agent can write overrides.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /home/coder/.codex-collimate /workspace /etc/codex \
    && chown -R coder:coder /home/coder /workspace \
    && chmod 755 /etc/codex

USER coder
WORKDIR /workspace

# Stay alive so the generator can seed config + run `codex exec` / the TUI via exec.
CMD ["sleep", "infinity"]
