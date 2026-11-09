#!/usr/bin/env bash
# Serve the assembled site (dist/) for a local look — a static server is
# all the demo needs, exactly like GitHub Pages.
#
#   PORT   port to listen on (default 4321)
set -euo pipefail
PLAYGROUND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$PLAYGROUND_DIR/dist"
[ -f "$DIST/sw.js" ] || { echo "serve.sh: $DIST is not built — run build.sh first" >&2; exit 1; }
PORT="${PORT:-4321}"
# serve's default "clean URLs" 301-redirects sandbox.html to /sandbox and
# drops the query string the renderer sandbox is parameterised with; a
# config file is the only way to switch that off — and with a config file
# serve stops resolving directory indexes itself, so the two index pages
# are rewritten explicitly. GitHub Pages serves the files as they are.
CONFIG="$(mktemp -t collimate-serve.XXXXXX.json)"
cat > "$CONFIG" <<'JSON'
{
  "cleanUrls": false,
  "rewrites": [
    { "source": "/", "destination": "/index.html" },
    { "source": "/playground", "destination": "/playground/index.html" }
  ]
}
JSON
echo "serving $DIST at http://127.0.0.1:$PORT/  (demo at /playground/)"
exec npx --yes serve@latest "$DIST" --listen "$PORT" --no-clipboard --config "$CONFIG"
