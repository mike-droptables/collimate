# SPDX-License-Identifier: MIT
"""Opaque socket keys preserve ended state across renderer mounts."""
from pathlib import Path
import subprocess


def test_socket_connection_key_tracks_dead_remount_and_redial__F0287_F1990(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    module = tmp_path / "session.mjs"
    build = subprocess.run([
        str(repo / "scripts/node_modules/esbuild/bin/esbuild"),
        str(repo / "packages/renderer-hooks/src/session.ts"),
        "--format=esm", "--target=es2022", f"--outfile={module}",
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    script = tmp_path / "run.mjs"
    script.write_text("""
import assert from 'node:assert/strict';
const sockets = [];
globalThis.WebSocket = class {
    static OPEN = 1;
    constructor() { sockets.push(this); }
    close() {}
};
const {openSessionSocket, isSessionDead} = await import(process.argv[2]);
const options = {url: 'ws://localhost/t', connectionKey: 'tmnl-test',
                 transientRetryMs: false, busyRetryMs: false};
const first = openSessionSocket(options);
assert.equal(sockets.length, 1);
sockets[0].onclose({code: 4404});
assert.equal(first.status, 'ended');
assert.equal(isSessionDead('tmnl-test'), true);
first.close();
const remount = openSessionSocket(options);
assert.equal(remount.status, 'ended');
assert.equal(sockets.length, 1);
const other = openSessionSocket({...options, connectionKey: 'chat-other'});
assert.equal(sockets.length, 2);
other.close();
remount.redial();
assert.equal(isSessionDead('tmnl-test'), false);
assert.equal(remount.status, 'connecting');
assert.equal(sockets.length, 3);
sockets[2].onclose({code: 4404});
assert.equal(isSessionDead('tmnl-test'), true);
remount.close();
""")
    result = subprocess.run(["node", str(script), module.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_open_then_close_without_a_frame_counts_as_failed_dial__F1988_F1989(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    module = tmp_path / 'session.mjs'
    build = subprocess.run([str(repo / 'scripts/node_modules/esbuild/bin/esbuild'),
                            str(repo / 'packages/renderer-hooks/src/session.ts'),
                            '--format=esm', '--target=es2022', f'--outfile={module}'],
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    script = tmp_path / 'unconfirmed.mjs'
    script.write_text('''
import assert from 'node:assert/strict';
const sockets=[],timers=[];
globalThis.setTimeout=fn=>{timers.push(fn);return timers.length;};globalThis.clearTimeout=()=>{};
globalThis.WebSocket=class {static OPEN=1;constructor(){sockets.push(this);}close(){}};
const {openSessionSocket}=await import(process.argv[2]);
const status=[];const connection=openSessionSocket({url:'ws://localhost/s',maxFailedOpens:2,transientRetryMs:0,onStatus:s=>status.push(s)});
sockets[0].onopen();sockets[0].onclose({code:1006});timers.shift()();
sockets[1].onopen();sockets[1].onclose({code:1006});
assert.equal(connection.status,'ended','an upgrade followed by refusal must not reset the failure cap');
assert.equal(timers.length,0);connection.close();
const confirmed=openSessionSocket({url:'ws://localhost/t',maxFailedOpens:2,transientRetryMs:0});
sockets[2].onopen();sockets[2].onclose({code:1006});timers.shift()();
sockets[3].onopen();sockets[3].onmessage({data:'accepted frame'});sockets[3].onclose({code:1006});
assert.equal(confirmed.status,'closed','a real received frame resets consecutive unconfirmed failures');confirmed.close();
''')
    result = subprocess.run(['node', str(script), module.as_uri()], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
