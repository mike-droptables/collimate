# Image for the agent generator's `qwen-code` backend (Qwen Code — the
# Gemini-CLI-lineage Alibaba coding CLI).
#
# Lands a non-root `coder` user (uid 1000, matching the host that owns the
# bind-mounted /workspace) with `@qwen-code/qwen-code` installed. PID 1 is
# `sleep infinity`; the engine-driven turns attach `qwen --acp
# --experimental-skills` via docker exec (the attached-stdio ACP transport),
# and `start: terminal` runs the CLI's own TUI. The container is the exec
# boundary. Auth/model ride the env trios the CLI's getAuthTypeFromEnv
# selects on (OPENAI_API_KEY/BASE_URL/MODEL · ANTHROPIC_* · GEMINI_*).
#
# Built on the shared collimate-linux-small base (node 24/npm + git + curl +
# ca-certificates), so this image only layers on the CLI itself.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`.
ARG QWEN_CODE_VERSION=0.26.1
RUN npm install -g @qwen-code/qwen-code@${QWEN_CODE_VERSION}

# Preserve the native executable while putting one private-write launcher on
# the shared ACP/terminal command path. Native arguments and policy stay intact.
COPY collimate-qwen /opt/collimate/qwen/collimate-qwen
RUN ln -s "$(readlink -f "$(command -v qwen)")" /opt/collimate/qwen/native-cli \
    && chmod 755 /opt/collimate/qwen/collimate-qwen \
    && rm -f /usr/local/bin/qwen \
    && ln -s /opt/collimate/qwen/collimate-qwen /usr/local/bin/qwen

# The stdio↔HTTP MCP proxy (ACP carrier B) — the collimate tool bridge stays
# reachable even when the harness advertises no HTTP MCP capability.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# uid 1000 must match the host that owns the bind-mounted /workspace so the
# CLI can write there. The per-cell harness home is bind-mounted over
# /home/coder/.qwen (the CLI's session/config store).
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace \
    && chown -R coder:coder /home/coder /workspace

USER coder
WORKDIR /workspace

# Stay alive so the generator can seed settings + attach ACP/TTY via exec.
CMD ["sleep", "infinity"]
