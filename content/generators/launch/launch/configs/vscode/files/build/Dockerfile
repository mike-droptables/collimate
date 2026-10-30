# Image for the `vscode` runner (built lazily by the launch generator in `app`
# mode from this build config folder; run.py tags it
# collimate-launch-build:<hash of this folder + the resolved tool versions>).
#
# Based on codercom/code-server with Python / Node / gh / uv / claude
# pre-installed, a marketplace patch so the integrated extension panel
# pulls from Microsoft's VS Code gallery instead of Open VSX, and a
# fixed default extension set baked in at build time. To add or remove
# default extensions edit the EXTENSIONS RUN step below and rebuild —
# the runtime entrypoint no longer installs anything (the headless
# install path was unreliable in practice; users add extras via the
# in-editor extensions panel).
ARG CODE_SERVER_VERSION=4.143.1
FROM codercom/code-server:${CODE_SERVER_VERSION}

USER root

# OS packages + gh CLI + Node. gh and jq are convenience tools for the
# user in the VS Code terminal. (The code-server version is resolved by run.py and participates
# in the content tag; apt/NodeSource packages refresh on an allowed rebuild.) uv and the claude CLI
# are installed in their own layers near the end, so a version bump
# rebuilds only those layers.
RUN apt-get update && apt-get install -y \
        git \
        curl \
        python3 \
        python3-pip \
        ca-certificates \
        gnupg \
        jq \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && curl -fsSL -o /tmp/nodesource_setup.sh https://deb.nodesource.com/setup_24.x \
    && bash /tmp/nodesource_setup.sh && rm -f /tmp/nodesource_setup.sh \
    && apt-get install -y nodejs \
    && node --version | grep -q '^v24\.' && npm --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

USER coder

# Point code-server at the Microsoft VS Code marketplace instead of
# Open VSX so the same extensions are available as in real VS Code.
ENV EXTENSIONS_GALLERY='{"serviceUrl": "https://marketplace.visualstudio.com/_apis/public/gallery", "cacheUrl": "https://vscode.blob.core.windows.net/gallery/index", "itemUrl": "https://marketplace.visualstudio.com/items", "controlUrl": "", "recommendationsUrl": ""}'

# Pre-create code-server's config with auth disabled. code-server 4.115
# ignores --config /dev/null and --auth CLI flags when a file at the
# default path exists (or generates one with auth: password on first
# run), so we ship the file ourselves with the values we want.
RUN mkdir -p /home/coder/.config/code-server && \
    printf 'auth: none\ncert: false\n' > /home/coder/.config/code-server/config.yaml

# Default user settings: dark mode, skip welcome page, no telemetry,
# autofetch on, workspace trust disabled (the project already runs in
# an isolated container, the trust prompt is redundant friction).
#   - claudeCode.preferredLocation:"panel" → Claude Code opens as a new
#     editor tab (the extension labels this "Panel (New Tab)").
#   - files.exclude hides the SDK-internal .collrun tree and the run
#     folders under _runs/ (host-owned run cards and live-surface pointers
#     such as app.website) so the explorer only shows the user's own
#     project files (the cell workspace is mounted directly
#     now, so these would otherwise be visible).
RUN mkdir -p /home/coder/.local/share/code-server/User && \
    printf '%s\n' '{' \
        '  "workbench.colorTheme": "Default Dark Modern",' \
        '  "workbench.startupEditor": "none",' \
        '  "git.autofetch": true,' \
        '  "telemetry.telemetryLevel": "off",' \
        '  "claudeCode.preferredLocation": "panel",' \
        '  "files.exclude": {' \
        '    "**/.collrun": true,' \
        '    "_runs": true' \
        '  },' \
        '  "security.workspace.trust.enabled": false,' \
        '  "security.workspace.trust.startupPrompt": "never",' \
        '  "security.workspace.trust.banner": "never",' \
        '  "security.workspace.trust.emptyWindow": false' \
        '}' \
        > /home/coder/.local/share/code-server/User/settings.json

# Bake the default extension set into the image. Each install runs
# against the Microsoft marketplace via EXTENSIONS_GALLERY set above.
# Add or remove ids here and rebuild the image; the entrypoint no
# longer installs anything at startup. Failures DON'T break the build
# (|| true) — a single rotted id shouldn't block image creation, but
# the build output should be checked for "[install warning]" lines.
RUN for ext in \
        "Anthropic.claude-code" \
        "ms-python.python" \
        "saoudrizwan.claude-dev" \
    ; do \
        echo "[ext] installing $ext"; \
        code-server --install-extension "$ext" || echo "[install warning] $ext failed"; \
    done

USER root

# uv and the claude CLI float: run.py resolves "latest" (or the runner's
# `packages` pin), passes it as a build arg and folds it into the content
# tag. The ARG defaults are only the fallback for a machine that never
# resolved one. Each ARG sits right before the RUN that uses it, so a
# release rebuilds only that layer and the small ones after it.
ARG UV_VERSION=0.13.5
RUN curl -LsSf -o /tmp/uv-install.sh "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    && env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh \
    && rm -f /tmp/uv-install.sh \
    && uv --version
ARG CLAUDE_CODE_VERSION=stable
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && claude --version

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Collimate Bootstrap extension. Installed into code-server's BUILTIN
# extensions dir (not the user dir) so it loads on every start without an
# extensions.json entry. On startup it opens Claude Code in a fresh
# conversation (gated by COLLIMATE_START_CLAUDE / the entrypoint marker) —
# code-server has no URL-command hook, so this is the reliable trigger.
COPY bootstrap-ext /usr/lib/code-server/lib/vscode/extensions/collimate-bootstrap
RUN chown -R coder:coder /usr/lib/code-server/lib/vscode/extensions/collimate-bootstrap
USER coder

# The editor runs as `coder`, which keeps the code-server base image's
# passwordless sudo (/etc/sudoers.d/nopasswd), so `sudo apt-get install`
# works in the VS Code terminal; the launch `sudo` setting governs terminal
# mode only. Whatever the editor or a sudo'd command writes into /workspace
# is handed back to the workspace owner when the app stops.
#
# The codercom/code-server base image ships an ENTRYPOINT that launches
# code-server directly. We need our own script to run first (Claude Code
# startup marker, git identity, background startup commands, dumb-init as
# PID 1), so override it.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
