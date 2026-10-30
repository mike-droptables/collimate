# Image for the agent generator's `goose` backend (all modes).
#
# Lands a non-root `coder` user (uid 1000, matching the host that owns the
# bind-mounted /workspace) with the goose CLI installed. PID 1 is
# `sleep infinity`; the generator execs `goose run` (headless oneshot/chat) or
# `goose session` (the live TTY) into it. goose's built-in `developer`
# extension is the in-container shell + file-edit surface; composition/web tools
# arrive over the streamable-http MCP bridge at runtime.
#
# Built on the shared collimate-linux-small base (which already has curl +
# ca-certificates), so this image only layers on bzip2 (goose ships as a
# bzip2 release tarball) + the goose binary. _ensure_image builds that base
# first from the _env_shared/base context when it isn't already present.
# The builder injects the resolved content-addressed base tag; the literal
# default keeps a hand-run `docker build` working.
ARG BASE=collimate-linux-small:v3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

# bzip2 unpacks goose's release archive; curl + ca-certificates are in the base.
# (No libxcb1: that's a goose DESKTOP dependency — the headless CLI links none.)
RUN apt-get update && apt-get install -y --no-install-recommends bzip2 \
    && rm -rf /var/lib/apt/lists/*

# uid 1000 must match the host that owns the bind-mounted /workspace so goose's
# developer extension can read/write there. The base has no uid-1000 user.
RUN useradd -m -s /bin/bash -u 1000 coder \
    && mkdir -p /workspace \
    && chown -R coder:coder /home/coder /workspace

# goose installs as root into /usr/local/bin (the installer's GOOSE_BIN_DIR),
# root-owned on purpose as with agy: `goose update` exists, and a
# `coder`-writable binary would let it (or the agent's developer shell) replace
# the pinned build mid-run. CONFIGURE=false skips the installer's interactive
# provider setup — the generator drives provider / model / keys entirely by
# GOOSE_* env at runtime. GOOSE_VERSION is honoured strictly: the installer
# downloads that exact tag and fails without it, and the script itself is
# fetched from the same tag. It is downloaded before it runs, not piped into
# bash: RUN has no pipefail, so a failed download would otherwise run an empty
# script, exit 0 and cache an image with no goose in it.
#
# The fallback must name a tag that still serves its asset: goose rotates
# release assets, and older tags' tarballs disappear (1.42.0's did, July
# 2026).
ARG GOOSE_VERSION=1.56.0
# cd /tmp first: the installer downloads the release tarball into its CWD. The
# build ends with a `goose --version` probe proving the pinned build landed;
# HOME is redirected for it so it leaves no root-owned config in the image.
RUN set -eux; \
    cd /tmp; \
    v="${GOOSE_VERSION#v}"; \
    curl -fsSL -o /tmp/goose-install.sh \
        "https://github.com/aaif-goose/goose/releases/download/v${v}/download_cli.sh"; \
    GOOSE_BIN_DIR=/usr/local/bin CONFIGURE=false GOOSE_VERSION="${v}" bash /tmp/goose-install.sh; \
    rm -f /tmp/goose-install.sh; \
    chown root:root /usr/local/bin/goose; \
    chmod 755 /usr/local/bin/goose; \
    HOME=/tmp/goose-probe goose --version | grep -F "${v}"; \
    rm -rf /tmp/goose-probe

# Containers have no OS keyring — keep goose from trying to reach one.
ENV GOOSE_DISABLE_KEYRING=1

# The stdio↔HTTP MCP proxy (ACP carrier B) — the collimate tool bridge stays
# reachable even when the harness advertises no HTTP MCP capability. Installed
# as root (the global npm prefix), like the base's own node tooling.
ARG MCP_REMOTE_VERSION=0.15.2
RUN npm install -g mcp-remote@${MCP_REMOTE_VERSION}
USER coder

WORKDIR /workspace

# Stay alive so the generator can exec `goose run` / `goose session`.
CMD ["sleep", "infinity"]
