"""Coverage for the session-socket SDK module on /api/sdk/session.js.

Spec: sdk/packages/renderer-hooks/src/session.ts. The module is
the ONE reconnect state machine every pointer renderer (terminal, assistant,
live browser) builds on — a missing export is a boot-time failure for all of
them.

It must stay framework-free so session helpers never introduce another
framework instance. An `import ... react`
sneaking into this module would silently split such a renderer's React tree.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest


def _fetch(client):
    r = client.get("/api/sdk/session.js")
    assert r.status_code == 200, r.text
    assert "javascript" in r.headers.get("content-type", "")
    return r.text


def test_module_is_framework_free(client):
    """No import statements at all — the module must not execute a React pin
    (or anything else) when imported by a renderer that brings its own
    framework versions."""
    body = _fetch(client)
    assert not re.search(r"^\s*import\s", body, re.MULTILINE), (
        "/api/sdk/session.js must stay dependency-free — see module docstring"
    )


_LISTENER_WINS_SCRIPT = r"""
const instances = [];
class FakeWebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.closed = false; instances.push(this); }
    close() { this.closed = true; this.readyState = 3; }
    send() {}
}
globalThis.WebSocket = FakeWebSocket;
const realSetTimeout = globalThis.setTimeout;
let timersAfterClose = 0;
let closedByListener = false;
globalThis.setTimeout = (fn, ms) => {
    if (closedByListener) timersAfterClose += 1;
    return realSetTimeout(fn, ms);
};
const { openSessionSocket } = await import(process.argv[2]);
const wait = (ms) => new Promise((r) => realSetTimeout(r, ms));
const fire = (w, code) => { w.closed = true; w.readyState = 3; w.onclose({ code }); };
const live = () => instances.filter((w) => !w.closed && w.onclose).length;
const out = {};

for (const [name, code, opts] of [
    ['redial_closed', 1006, { transientRetryMs: 10 }],
    ['redial_busy', 4409, { busyRetryMs: 10 }],
]) {
    instances.length = 0;
    let acted = false;
    const s = openSessionSocket({
        url: 'ws://x/' + name, ...opts,
        onStatus: (st) => { if (!acted && (st === 'closed' || st === 'busy')) { acted = true; s.redial(); } },
    });
    fire(instances[0], code);
    await wait(50);
    out[name] = { sockets: instances.length, live: live() };
    s.close();
}

instances.length = 0;
const s = openSessionSocket({
    url: 'ws://x/close', transientRetryMs: 10,
    onStatus: (st) => { if (st === 'closed') { s.close(); closedByListener = true; } },
});
fire(instances[0], 1006);
await wait(50);
out.close_in_listener = { sockets: instances.length, timers: timersAfterClose, status: s.status };
console.log(JSON.stringify(out));
"""


def test_listener_redial_or_close_in_on_status_wins_over_the_retry(client, tmp_path):
    """onStatus is user code: a redial() or close() it makes while handling
    'closed'/'busy' must not be followed by the machine's own scheduled retry
    (which would orphan a second live socket, or arm a timer after close())."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    mod = tmp_path / "session.mjs"
    mod.write_text(_fetch(client))
    script = tmp_path / "run.mjs"
    script.write_text(_LISTENER_WINS_SCRIPT)
    proc = subprocess.run(
        [node, str(script), mod.as_uri()], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    for name in ("redial_closed", "redial_busy"):
        assert out[name] == {"sockets": 2, "live": 1}, (name, out[name])
    assert out["close_in_listener"]["sockets"] == 1, out
    assert out["close_in_listener"]["timers"] == 0, out


def test_served_session_matches_canonical_sdk_built_bytes__F1990_F1883(client):
    from collimate.shared import shared_path
    assert _fetch(client).encode() == shared_path("renderer-runtime", "session.js").read_bytes()


@pytest.mark.parametrize("verifier", ["verify_runtime_resources", "verify_prelude_resources"])
def test_a_stale_built_runtime_or_prelude_refuses_startup(api_app, monkeypatch, verifier):
    """The SDK's runtime and prelude are git-ignored build outputs in a
    checkout; served bytes that no longer match their manifest refuse the
    start, naming the rebuild, instead of being served."""
    from fastapi.testclient import TestClient
    from collimate import renderer_runtime
    import main

    def stale(root=None):
        raise ValueError("Renderer runtime resource mismatch: react.js")

    monkeypatch.setattr(renderer_runtime, verifier, stale)
    with pytest.raises(main.StartupRefused, match=r"resource mismatch: react\.js .*sdk/scripts/build_outputs\.py"):
        with TestClient(api_app):
            pass
