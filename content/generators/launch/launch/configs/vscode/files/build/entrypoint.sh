#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] starting"
echo "[entrypoint] PORT=${PORT:-unset}"

# Open the cell workspace IN PLACE. /workspace is the cell's own workspace
# directory bind-mounted read-write, so every edit code-server makes lands
# in the cell and syncs back live — no copy, no manual snapshot. (We used to
# copy /workspace into a private project dir behind a read-only mount, which
# made the files one-way and locked.)
PROJECT_DIR=/workspace
mkdir -p "${PROJECT_DIR}"

# Claude Code startup marker. The generator passes COLLIMATE_START_CLAUDE
# (1/0); the builtin bootstrap extension reads it — and this marker file as a
# fallback — to decide whether to open Claude Code on first paint.
if [[ "${COLLIMATE_START_CLAUDE:-1}" == "1" ]]; then
    mkdir -p /home/coder/.collimate && touch /home/coder/.collimate/open-claude
    echo "[entrypoint] Claude Code will open on startup"
else
    rm -f /home/coder/.collimate/open-claude 2>/dev/null || true
    echo "[entrypoint] starting a plain VS Code window"
fi

# Configure git author identity if provided. (GitHub credentials are the
# launch generator's to write, with one exec, once code-server answers.)
if [[ -n "${GIT_USER_NAME:-}" ]]; then
    git config --global user.name "${GIT_USER_NAME}"
    echo "[entrypoint] git user.name = ${GIT_USER_NAME}"
fi
if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    git config --global user.email "${GIT_USER_EMAIL}"
    echo "[entrypoint] git user.email = ${GIT_USER_EMAIL}"
fi

# (Extensions are baked into the image at build time — see the Dockerfile's
# EXTENSIONS RUN block. The previous runtime install path was unreliable.)

# Run startup commands in background, in parallel with code-server. With a
# GitHub login bound (COLLIMATE_AWAIT_GIT_LOGIN=1) they first wait, bounded,
# for the marker the launch generator touches once it has written the
# credentials, so a startup `git clone` of a private repo authenticates.
if [[ -n "${STARTUP_COMMANDS:-}" ]]; then
    echo "[entrypoint] launching startup commands in background"
    (
        cd "${PROJECT_DIR}"
        if [[ "${COLLIMATE_AWAIT_GIT_LOGIN:-0}" == "1" ]]; then
            for ((i = 0; i < 180; i++)); do
                [[ -e /home/coder/.collimate/git-login-ready ]] && break
                sleep 1
            done
        fi
        python3 - <<'PYEOF'
import json, os, subprocess, sys
cmds = json.loads(os.environ.get("STARTUP_COMMANDS", "[]"))
for c in cmds:
    print(f"[startup] running: {c}", flush=True)
    rc = subprocess.call(["bash", "-lc", c])
    if rc != 0:
        print(f"[startup] command failed with exit {rc}: {c}", flush=True)
        sys.exit(rc)
print("[startup] all commands completed", flush=True)
PYEOF
    ) &
    echo "[entrypoint] startup pid: $!"
fi

# Launch code-server under dumb-init (as the base image's own entrypoint does)
# so PID 1 reaps orphaned processes and forwards SIGTERM on stop. Background
# startup commands stay children of PID 1.
echo "[entrypoint] launching code-server on 0.0.0.0:${PORT} for ${PROJECT_DIR}"
exec dumb-init code-server \
    --bind-addr "0.0.0.0:${PORT}" \
    "${PROJECT_DIR}"
