# Vendored ACP registry snapshot — the `test_agent_matrix` third leg

A point-in-time copy of the [ACP registry](https://github.com/agentclientprotocol/registry)
facts the agent backend matrix is pinned against (agent-v2 §6.3): the
protocol matrix (per-agent probe results), the quarantine list, and the
per-agent `agent.json` invocation records for exactly the registry ids the
roster maps onto.

| File | Upstream source |
|---|---|
| `protocol-matrix.json` | `.protocol-matrix/latest.json` |
| `quarantine.json` | `quarantine.json` |
| `agents.json` | `{<id>/agent.json}` assembled per roster registry id |

The roster ids are exactly the backends whose ACP adapter comes FROM the
registry. A native ACP harness the registry does not list yet (`hermes`) has
no row: it says why in its adapter's `unregistered` and is re-checked against
every new snapshot. (`antigravity` runs Google's own `antigravity-acp`
server, pinned in its image by version and the manifest's sha256s; its Linux
records carry the `--uid=` argument, the others none.)

Vendored 2026-10-25 (matrix date 2026-10-25). `mistral-vibe` cleared
quarantine in this snapshot and joined the roster, with
`github-copilot-cli`. Re-vendor deliberately — an update is an
adapter-facts review, not a refresh chore: the drift guard exists so a
roster assumption (an agent's invocation args, its loadSession capability,
its quarantine state) can't silently rot. The protocol matrix is also the
pre-check for a new harness: its `initialize`, `loadSession` and session
capabilities are what the adapter's declaration and the client's capability
probe must agree with.

```bash
R=https://raw.githubusercontent.com/agentclientprotocol/registry/main
curl -sL $R/.protocol-matrix/latest.json -o protocol-matrix.json
curl -sL $R/quarantine.json -o quarantine.json
python3 - <<'PY'
import json, urllib.request
ids = ["antigravity-acp", "claude-acp", "codex-acp", "goose", "opencode", "qwen-code",
       "cline", "pi-acp", "mistral-vibe", "github-copilot-cli"]
base = "https://raw.githubusercontent.com/agentclientprotocol/registry/main"
out = {i: json.load(urllib.request.urlopen(f"{base}/{i}/agent.json")) for i in ids}
with open("agents.json", "w") as fh:
    json.dump(out, fh, indent=1, sort_keys=True); fh.write("\n")
PY
```
