# Image for the agent generator's `antigravity` backend.
#
# Lands a non-root `coder` user (uid 1000, matching the host that owns the
# bind-mounted /workspace) with TWO Google binaries:
#
#   * Google's official ACP server `antigravity-acp` (registry id of the same
#     name; `agy_acp_server.par` plus its `localharness_external` agent
#     harness), the chat/annotation legs' adapter, spawned per run through
#     `docker exec`. It speaks ACP itself, so Collimate ships no shim;
#   * Google's `agy` CLI, the terminal (TTY) leg only.
#
# PID 1 is `sleep infinity`; the generator seeds the per-cell ~/.gemini
# (settings.json for either surface, the global rules file, mcp_config.json)
# and then attaches the `agy` TUI (terminal mode) or spawns the ACP server.
# The runner grant's `commands` level is enforced by the ACP client's own gate
# on the server's `session/request_permission` (and, defence in depth, by
# Collimate's PreToolUse hook `collimate-agy-hook`, below, which the server
# also runs from the global hooks.json); on the TTY by agy's settings
# permissions and that hook.
#
# Built on the shared collimate-linux-small base (node 24/npm + git + curl +
# ca-certificates), so this image only layers on the two Google binaries and
# the MCP proxy. _ensure_image builds that base first from the _env_shared/base
# context when it isn't already present.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

# The ACP server, pinned: version and the registry manifest's sha256 for each
# architecture (https://raw.githubusercontent.com/agentclientprotocol/registry/
# main/antigravity-acp/agent.json). Unlike the floating CLIs below, the archive
# is verified before anything is unpacked; a mismatch fails the build. Linux
# runs `./agy_acp_server.par --uid=` per that manifest. The Python payload is
# a ~0.9 GB self-contained archive: the image is large by that much. Both files
# are root-owned under /opt/agy-acp, so the agent cannot replace the pinned
# server (and with it the protocol and the permission flow) mid-run. The base
# has no `unzip`; python's zipfile unpacks the archive, and the executable bits
# it does not restore are set explicitly.
ARG AGY_ACP_VERSION=1.4.3
ARG AGY_ACP_SHA256_AMD64=0634ce5306ba92c932945455e7a83fda78e2d99c33fa8aa6ee0566206a382714
ARG AGY_ACP_SHA256_ARM64=05c153443b0b1175151c760eeddea538950bc9843e317d54786a1f03670182bc
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) suffix="x86_64"; sha="${AGY_ACP_SHA256_AMD64}" ;; \
        arm64) suffix="arm64"; sha="${AGY_ACP_SHA256_ARM64}" ;; \
        *) echo "no antigravity-acp build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/agy-acp.zip \
        "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-${AGY_ACP_VERSION}-linux-${suffix}.zip"; \
    echo "${sha}  /tmp/agy-acp.zip" | sha256sum -c -; \
    mkdir -p /opt/agy-acp; \
    python3 -I -m zipfile -e /tmp/agy-acp.zip /opt/agy-acp; \
    rm -f /tmp/agy-acp.zip; \
    chown -R root:root /opt/agy-acp; \
    chmod 755 /opt/agy-acp /opt/agy-acp/agy_acp_server.par /opt/agy-acp/localharness_external; \
    test -x /opt/agy-acp/agy_acp_server.par && test -x /opt/agy-acp/localharness_external

# The ARG default is the offline fallback: cli_container._requested_versions
# builds the latest release (folded into the image tag) unless the runner
# pins a version in `packages:`.
#
# The terminal (TTY) leg only: the chat and annotation legs run the ACP server
# above, not this binary. agy ships as a GitHub release tarball holding ONE 213 MB static Go binary
# named `antigravity` (no shared libraries needed); we rename it to `agy`, the
# name every Collimate call site and every piece of Google's own documentation
# uses. Same payload as Google's own updater manifest at
# antigravity-cli-auto-updater-…run.app/manifests/linux_amd64.json.
#
# No checksum pinned here, on the goose precedent: the version ARG floats with
# the runner's `packages:` request, so a hard-coded digest would break every
# bump; HTTPS to github.com plus the pinned release TAG are the integrity
# boundary. Root-owned under /usr/local/bin on purpose — `agy update` exists,
# and a `coder`-writable binary would let the CLI silently replace the pinned
# build mid-run.
#
# The build ends by running `agy --version`: proof that the downloaded asset
# really is the CLI, before the image is ever tagged. HOME is redirected for
# that probe so it cannot leave a root-owned ~/.gemini behind in the image —
# the generator stages vendor state separately from protected configuration.
ARG AGY_VERSION=1.4.7
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) asset="agy_cli_linux_x64.tar.gz" ;; \
        arm64) asset="agy_cli_linux_arm64.tar.gz" ;; \
        *) echo "no agy release asset for architecture ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/agy.tar.gz \
        "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/${asset}"; \
    tar -xzf /tmp/agy.tar.gz -C /usr/local/bin antigravity; \
    rm -f /tmp/agy.tar.gz; \
    mv /usr/local/bin/antigravity /usr/local/bin/agy; \
    chown root:root /usr/local/bin/agy; \
    chmod 755 /usr/local/bin/agy; \
    HOME=/tmp/agy-probe agy --version; \
    rm -rf /tmp/agy-probe

# The stdio<->HTTP MCP proxy (ACP carrier B) — the collimate tool bridge stays
# reachable even when a harness advertises no HTTP MCP capability. (The ACP
# server takes an http `mcpServers` entry itself, so it does not use it.)
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}

# Collimate's PreToolUse hook: denies the exec tools of agy and of the ACP
# server (both read the global hooks.json) under commands: deny, and under ask
# answers force_ask (agy's own prompt in the TUI; the ACP server treats it as
# no opinion and asks the client through session/request_permission).
# Root-owned so the agent cannot rewrite the gate it runs behind; the host
# mounts declared global hooks.json content read-only into a protected
# directory outside the workspace.
COPY agy_hook.py /usr/local/bin/collimate-agy-hook
RUN chmod 755 /usr/local/bin/collimate-agy-hook

# Only vendor state links into the run's private workspace. Keep every
# ancestor of hooks.json root-owned: a writable parent would let the agent
# replace a protected file or directory. The TTY leg's MCP config is updated
# in place at its precreated file; its parent remains protected from
# replacement. Native project state has its own writable child; it cannot
# replace protected siblings.
#
# GEMINI_HOME is the ACP server's whole tree (settings.json, acp_token.json,
# conversations/, brain/ under antigravity-acp/; hooks and rules under config/).
# Its private directory is coder-writable and nothing else is; the host links
# the per-cell harness home onto it. The server runs under Docker's default
# seccomp profile (no security-opt override anywhere in the SDK).
ENV GEMINI_HOME=/home/coder/.gemini
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace /home/coder/.gemini/config/rules /home/coder/.gemini/config/projects \
        /home/coder/.gemini/antigravity-acp \
    && touch /home/coder/.gemini/config/mcp_config.json \
    && chown root:root /home/coder /home/coder/.gemini /home/coder/.gemini/config /home/coder/.gemini/config/rules \
    && chmod 755 /home/coder /home/coder/.gemini /home/coder/.gemini/config /home/coder/.gemini/config/rules \
    && chown coder:coder /workspace /home/coder/.gemini/config/mcp_config.json \
    && chmod 600 /home/coder/.gemini/config/mcp_config.json \
    && chown coder:coder /home/coder/.gemini/config/projects \
    && chmod 700 /home/coder/.gemini/config/projects \
    && chown coder:coder /home/coder/.gemini/antigravity-acp \
    && chmod 700 /home/coder/.gemini/antigravity-acp

USER coder
WORKDIR /workspace

# Stay alive so the generator can seed the home + attach the TUI or the ACP
# server via exec.
CMD ["sleep", "infinity"]
