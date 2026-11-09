// SPDX-License-Identifier: MIT
//
// terminal.test.ts — the terminal renderer's pure model: the `.terminal`
// pointer parse (its required session coords and the optional toolbar
// `buttons`, each with an optional tooltip and confirm), which buttons the
// toolbar offers, and a button's click — confirmed first when it asks, then
// emitted as a renderer action; the mono face's load timing (S03); and the
// skin (R05): the xterm theme from the skin tokens over one ANSI palette per
// scheme, the chrome on tokens, no colour literal outside the palette. Runs
// under node:test via the tsx loader against a stub-bundled build of
// main.tsx (a renderer must be a single self-contained module, so
// tests/test_terminal_renderer_model.py bundles it first); the bundle path
// arrives via TERMINAL_RENDERER_BUNDLE. The xterm/PTY side has no Node DOM
// and is exercised live in the app.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.TERMINAL_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'TERMINAL_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_terminal_renderer_model.py does this).',
    );
}

let m: any;

before(async () => {
    m = await import(pathToFileURL(BUNDLE!).href);
});

const POINTER = { terminal_id: 's1', ws_path: '/api/terminals/s1', token: 't', label: 'claude' };

test('parseContent reads the session coords and defaults the rest', () => {
    assert.deepEqual(m.parseContent(JSON.stringify(POINTER)), { ...POINTER, buttons: [] });
    const bare = m.parseContent(JSON.stringify({ terminal_id: 's1', ws_path: '/w' }));
    assert.equal(bare.token, '');
    assert.equal(bare.label, 'terminal');
    assert.deepEqual(bare.buttons, []);
});

test('parseContent rejects junk and a pointer without session coords', () => {
    assert.equal(m.parseContent('not json'), null);
    assert.equal(m.parseContent(JSON.stringify({ ws_path: '/w' })), null);
    assert.equal(m.parseContent(JSON.stringify({ terminal_id: 's1' })), null);
});

// A generic button that asks before it emits (m is loaded in before()).
const restart = () => ({ id: 'restart', label: 'Restart', confirm: 'Restart it?' });

test('a button carries its own title and confirm (strings only)', () => {
    // A generator words them itself; the renderer adds nothing of its own.
    const p = m.parseContent(JSON.stringify({
        ...POINTER,
        buttons: [
            { id: 'restart', label: 'Restart', title: 'Start the program again.', confirm: 'Restart it?' },
            { id: 'go', label: 'Go', title: 'Go now', confirm: 7 },
            { id: 'x', label: 'X', title: '', confirm: null },
        ],
    }));
    assert.deepEqual(p.buttons, [
        { id: 'restart', label: 'Restart', title: 'Start the program again.', confirm: 'Restart it?' },
        { id: 'go', label: 'Go', title: 'Go now' },
        { id: 'x', label: 'X' },
    ]);
});

test('toolbarButtons: none once the session has ended', () => {
    const buttons = [{ id: 'capture_chat', label: 'Capture Chat' }, restart()];
    for (const status of ['idle', 'connecting', 'open', 'closed', 'busy']) {
        assert.deepEqual(m.toolbarButtons(buttons, status), buttons, status);
    }
    // Nothing is left to take the action — no button that can only fail.
    assert.deepEqual(m.toolbarButtons(buttons, 'ended'), []);
});

test('clickStep: a confirm is asked first, then its own button emits', () => {
    const b = m.parseContent(JSON.stringify({ ...POINTER, buttons: [restart()] })).buttons[0];
    assert.equal(m.clickStep(b, null), 'confirm');
    assert.equal(m.clickStep(b, 'restart'), 'emit');
    // Confirming another button does not confirm this one.
    assert.equal(m.clickStep(b, 'capture_chat'), 'confirm');
    // No confirm → one click emits (Capture Chat).
    assert.equal(m.clickStep({ id: 'capture_chat', label: 'Capture Chat' }, null), 'emit');
});

test('parseContent drops a malformed button', () => {
    const p = m.parseContent(JSON.stringify({
        ...POINTER,
        buttons: [{ id: 'restart' }, { label: 'x' }, null, 'restart', { id: 'ok', label: 'OK' }],
    }));
    assert.deepEqual(p.buttons, [{ id: 'ok', label: 'OK' }]);
});

test('clicking a button emits its id as the action', async () => {
    const calls: string[] = [];
    const api = { emitAction: async (id: string) => { calls.push(id); return true; } };
    assert.equal(await m.emitButton(api, 'restart'), true);
    assert.equal(await m.emitButton(api, 'capture_chat'), true);
    assert.deepEqual(calls, ['restart', 'capture_chat']);
});

test('a refused or failed click reports false (the toolbar says "<label> failed")', async () => {
    // No active run: the host's action endpoint 404s and emitAction is false.
    assert.equal(await m.emitButton({ emitAction: async () => false }, 'restart'), false);
    assert.equal(await m.emitButton({ emitAction: async () => { throw new Error('net'); } }, 'restart'), false);
    assert.equal(await m.emitButton({}, 'restart'), false);
});

test('a server error frame renders as a red bracketed message', () => {
    // The server sends {"t":"error","msg":...} as a text frame before it
    // closes the socket; the user sees the message, not the raw JSON.
    const out = m.textFrameOutput(JSON.stringify({ t: 'error', msg: "the terminal's process has ended" }));
    assert.equal(out, "\r\n\x1b[31m[the terminal's process has ended]\x1b[0m\r\n");
    assert.doesNotMatch(out, /"t"/);
    assert.match(m.textFrameOutput(JSON.stringify({ t: 'error' })), /\[terminal error\]/);
});

test('any other text frame is written through unchanged', () => {
    assert.equal(m.textFrameOutput('plain text\r\n'), 'plain text\r\n');
    const other = JSON.stringify({ t: 'resize', cols: 80, rows: 24 });
    assert.equal(m.textFrameOutput(other), other);
    assert.equal(m.textFrameOutput('null'), 'null');
    assert.equal(m.textFrameOutput('42'), '42');
});

test('parseContent uses the terminal_id pointer shape (F0287)', () => {
    const parsed = m.parseContent(JSON.stringify(POINTER));
    assert.equal(parsed.terminal_id, POINTER.terminal_id);
    assert.deepEqual(parsed, { ...POINTER, buttons: [] });
});

// Exercise the renderer's actual socket wrapper with the real shared socket
// state machine. Only the browser transport and clock are controlled here.
import { openSessionSocket } from '../../../sdk/packages/renderer-hooks/src/session.ts';

class TestSocket {
    static OPEN = 1;
    static all: TestSocket[] = [];
    readyState = 0;
    sent: unknown[] = [];
    onopen: any; onmessage: any; onerror: any; onclose: any;
    constructor(_url: string) { TestSocket.all.push(this); }
    open() { this.readyState = 1; this.onopen?.({}); }
    send(value: unknown) { this.sent.push(value); }
    close() { this.readyState = 3; }
    ended(code: number) { this.readyState = 3; this.onclose?.({code}); }
}

function transport(t: any) {
    const previous = globalThis.WebSocket;
    globalThis.WebSocket = TestSocket as any;
    TestSocket.all = [];
    t.mock.timers.enable({apis: ['setTimeout']});
    t.after(() => { globalThis.WebSocket = previous; });
}

test('P4F08 1013 quietly reconnects once without replaying stdin', (t) => {
    transport(t);
    const statuses: string[] = [];
    let reset = 0;
    const socket = m.openTerminalSocket({url: 'ws://test', onStatus: (s: string) => statuses.push(s),
        onRetry: () => reset++}, openSessionSocket);
    const first = TestSocket.all[0];
    first.open();
    assert.equal(socket.send('once'), true);
    first.ended(1013);
    assert.equal(statuses.at(-1), 'reconnecting');
    assert.equal(socket.send('while detached'), false);
    assert.doesNotMatch(m.BACKPRESSURE_NOTICE, /\x1b\[31m|error/i);
    t.mock.timers.tick(999);
    assert.equal(TestSocket.all.length, 1);
    t.mock.timers.tick(1);
    assert.equal(TestSocket.all.length, 2);
    assert.equal(reset, 1);
    const next = TestSocket.all[1];
    next.open();
    assert.deepEqual(next.sent, []);
    assert.equal(socket.send('new input'), true);
    assert.deepEqual(first.sent, ['once']);
    assert.deepEqual(next.sent, ['new input']);
    socket.close();
});

test('P4F08 ordinary closed busy and dead codes never auto-retry', (t) => {
    transport(t);
    for (const code of [1000, 1006, 1011, 4409, 4401, 4404]) {
        const socket = m.openTerminalSocket({url: 'ws://test'}, openSessionSocket);
        const count = TestSocket.all.length;
        TestSocket.all.at(-1)!.ended(code);
        t.mock.timers.tick(10000);
        assert.equal(TestSocket.all.length, count, String(code));
        socket.close();
    }
});

test('P4F08 disposal and manual reconnect cancel the queued retry', (t) => {
    transport(t);
    let socket = m.openTerminalSocket({url: 'ws://test'}, openSessionSocket);
    TestSocket.all[0].ended(1013);
    socket.close();
    t.mock.timers.tick(10000);
    assert.equal(TestSocket.all.length, 1);
    socket = m.openTerminalSocket({url: 'ws://test'}, openSessionSocket);
    TestSocket.all[1].ended(1013);
    socket.redial();
    assert.equal(TestSocket.all.length, 3);
    t.mock.timers.tick(10000);
    assert.equal(TestSocket.all.length, 3);
    TestSocket.all[2].ended(4404);
    t.mock.timers.tick(10000);
    assert.equal(TestSocket.all.length, 3);
    socket.close();
});

// ---------------------------------------------------------------------------
// The starting pointer and the input typed before the terminal exists (GN-03)
// ---------------------------------------------------------------------------

test('a starting pointer parses as starting, never as a session (GN-03)', () => {
    assert.deepEqual(m.parseStarting(JSON.stringify({ starting: true, label: 'Claude Code' })),
                     { label: 'Claude Code' });
    assert.deepEqual(m.parseStarting(JSON.stringify({ starting: true })), { label: '' });
    // The placeholder has no session coords, so it is not a session...
    assert.equal(m.parseContent(JSON.stringify({ starting: true, label: 'x' })), null);
    // ...and a live pointer, junk and a non-true flag are not starting.
    for (const raw of [JSON.stringify(POINTER), JSON.stringify({ starting: 'yes' }), 'not json', '[]', 'null']) {
        assert.equal(m.parseStarting(raw), null, raw);
    }
    assert.equal(m.statusColor('starting'), 'var(--ink-2)');
});

test('keystrokes typed while starting are sent once, in order, by the first session (GN-03)', () => {
    const key = 'cell-a:_runs/agent-1/terminal/claude-code.terminal';
    assert.equal(m.hasPendingInput(key), false);
    assert.equal(m.queueInput(key, 'ls'), 'queued');
    assert.equal(m.queueInput(key, ' -la\r'), 'queued');
    // Typed into the live session before its first output: behind the queue.
    const sent: string[] = [];
    const send = (d: string) => { sent.push(d); return true; };
    assert.equal(m.routeInput(key, 'echo hi\r', send), 'queued');
    assert.deepEqual(sent, []);
    // The first output frame flushes the whole queue as one write...
    assert.equal(m.flushPendingInput(key, send), true);
    assert.deepEqual(sent, ['ls -la\recho hi\r']);
    // ...once: a retry's first output (1013) or a reconnect finds nothing.
    assert.equal(m.flushPendingInput(key, send), false);
    assert.deepEqual(sent, ['ls -la\recho hi\r']);
    // Afterwards keystrokes go straight out.
    assert.equal(m.routeInput(key, 'q', send), 'sent');
    assert.deepEqual(sent, ['ls -la\recho hi\r', 'q']);
    // Another file's queue is its own.
    assert.equal(m.pendingInputKey({ getCellId: () => 'c', getFilename: () => 'a.terminal' }), 'c:a.terminal');
    assert.equal(m.pendingInputKey(null), ':');
});

test('the pending input is capped at 64 KiB with one overflow notice (GN-03)', () => {
    const key = 'cell-b:t.terminal';
    assert.equal(m.PENDING_INPUT_CAP, 64 * 1024);
    assert.equal(m.queueInput(key, 'x'.repeat(m.PENDING_INPUT_CAP - 1)), 'queued');
    assert.equal(m.queueInput(key, 'é'), 'overflow', 'bytes, not characters, are counted');
    assert.equal(m.queueInput(key, 'y'), 'dropped');
    assert.equal(m.routeInput(key, 'z', () => true), 'dropped');
    assert.match(m.PENDING_OVERFLOW_NOTICE, /64 KiB/);
    assert.doesNotMatch(m.PENDING_OVERFLOW_NOTICE, /\x1b\[31m/);
    const sent: string[] = [];
    m.flushPendingInput(key, (d: string) => { sent.push(d); return true; });
    assert.equal(sent.length, 1);
    assert.equal(sent[0].length, m.PENDING_INPUT_CAP - 1);
});

test('input the run never takes is dropped with a notice, not silently (GN-03)', () => {
    const key = 'cell-d:t.terminal';
    m.queueInput(key, 'abc');
    assert.equal(m.dropPendingInput(key), 3);
    assert.equal(m.hasPendingInput(key), false);
    assert.equal(m.dropPendingInput(key), 0);
    assert.match(m.droppedInputNotice(3), /3 bytes of typed input not sent/);
    assert.match(m.droppedInputNotice(1), /1 byte of typed input not sent/);
});

test('the 1013 no-replay rule holds beside the pending queue (GN-03)', (t) => {
    // Input sent on an attach that is then dropped with 1013 is not replayed
    // on the retry; the pending queue was already sent and is gone.
    transport(t);
    const key = 'cell-c:t.terminal';
    m.queueInput(key, 'early');
    const socket = m.openTerminalSocket({url: 'ws://test'}, openSessionSocket);
    const first = TestSocket.all[0];
    first.open();
    assert.equal(m.flushPendingInput(key, (d: string) => socket.send(d)), true);
    first.ended(1013);
    t.mock.timers.tick(1000);
    const next = TestSocket.all[1];
    next.open();
    assert.equal(m.flushPendingInput(key, (d: string) => socket.send(d)), false);
    assert.deepEqual(first.sent, ['early']);
    assert.deepEqual(next.sent, []);
    socket.close();
});

// A FontFaceSet stand-in: `loaded` holds the families whose faces are in;
// load() resolves when `release()` is called (or at once with `instant`).
function fakeFonts(opts: { loaded?: string[]; instant?: boolean; fail?: boolean } = {}) {
    const loaded = new Set(opts.loaded ?? []);
    const familyOf = (spec: string) => spec.replace(/^(bold )?\d+px /, '');
    const pending: Array<() => void> = [];
    const loads: string[] = [];
    return {
        loads,
        check: (spec: string) => loaded.has(familyOf(spec)),
        load: (spec: string) => {
            loads.push(spec);
            if (opts.fail) return Promise.reject(new Error('network'));
            return new Promise<void>((resolve) => {
                const done = () => { loaded.add(familyOf(spec)); resolve(); };
                if (opts.instant) done(); else pending.push(done);
            });
        },
        release: () => { for (const done of pending.splice(0)) done(); },
    };
}

const PLEX = '"IBM Plex Mono", ui-monospace, monospace';

test('xterm measures the fallback until the skin face has loaded (S03)', () => {
    // fonts.css uses font-display: swap, so a face that is not in yet would
    // be measured as the fallback and then draw off the grid.
    assert.equal(m.measurableFamily(fakeFonts(), PLEX, 13), m.MONO_FALLBACK);
    assert.equal(m.measurableFamily(fakeFonts({ loaded: [PLEX] }), PLEX, 13), PLEX);
    assert.deepEqual(m.monoFaceSpecs(PLEX, 13), [`13px ${PLEX}`, `bold 13px ${PLEX}`]);
});

test('the skin face reaches xterm only after its regular and bold faces load (S03)', async () => {
    const fonts = fakeFonts();
    const term = { options: { fontFamily: m.MONO_FALLBACK, fontSize: 15 } };
    const settled = m.settleMonoFont(term, fonts, () => PLEX);
    await Promise.resolve();
    assert.equal(term.options.fontFamily, m.MONO_FALLBACK, 'not before the face is in');
    assert.deepEqual(fonts.loads, [`15px ${PLEX}`, `bold 15px ${PLEX}`]);
    fonts.release();
    assert.equal(await settled, true, 'a changed family asks the caller to refit');
    assert.equal(term.options.fontFamily, PLEX);
    // Already on the face: nothing to re-measure.
    assert.equal(await m.settleMonoFont(term, fakeFonts({ instant: true }), () => PLEX), false);
});

test('a skin switch while a face loads leaves the family to the newer call (S03)', async () => {
    const fonts = fakeFonts();
    const term = { options: { fontFamily: m.MONO_FALLBACK, fontSize: 13 } };
    let family = PLEX;
    const first = m.settleMonoFont(term, fonts, () => family);
    family = '"JetBrains Mono", monospace';
    fonts.release();
    assert.equal(await first, false);
    assert.equal(term.options.fontFamily, m.MONO_FALLBACK);
});

test('a face that fails to load still sets the family, which names its fallbacks (S03)', async () => {
    const term = { options: { fontFamily: m.MONO_FALLBACK, fontSize: 13 } };
    assert.equal(await m.settleMonoFont(term, fakeFonts({ fail: true }), () => PLEX), true);
    assert.equal(term.options.fontFamily, PLEX);
});

// ---------------------------------------------------------------------------
// The xterm theme and the header chrome on the skin tokens (R05). The
// snapshots are what api.getTheme() carries for the three built-in skins
// (frontend/src/lib/themes.ts), reduced to the tokens the terminal reads.
// ---------------------------------------------------------------------------

const SKINS: Record<string, { scheme: 'dark' | 'light'; vars: Record<string, string> }> = {
    night: { scheme: 'dark', vars: {
        '--code-bg': '#121008', '--ink': '#e2ddd4', '--brass': '#c69d64',
        '--brass-soft': 'rgba(198, 157, 100, 0.16)', '--teal': '#7fb0ab' } },
    'night-cool': { scheme: 'dark', vars: {
        '--code-bg': '#131920', '--ink': '#dbdfe3', '--brass': '#b9966b',
        '--brass-soft': 'rgba(185, 150, 107, 0.14)', '--teal': '#76a9a6' } },
    paper: { scheme: 'light', vars: {
        '--code-bg': '#efede7', '--ink': '#1e2227', '--brass': '#8a6d3b',
        '--brass-soft': 'rgba(138, 109, 59, 0.12)', '--teal': '#2e7773' } },
};

function luminance(hex: string): number {
    const n = hex.replace('#', '').slice(0, 6);
    const lin = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255)
        .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}
function contrast(a: string, b: string): number {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
}

test('the xterm theme resolves the skin tokens: code ground, ink, teal cursor, brass-soft selection (R05)', () => {
    const t = m.buildXtermTheme(SKINS.night);
    assert.equal(t.background, '#121008ff', 'the ground is --code-bg, not black');
    assert.equal(t.cursorAccent, t.background);
    assert.equal(t.foreground, '#e2ddd4ff');
    assert.equal(t.cursor, '#7fb0abff', 'the cursor is teal (live), not brass');
    // rgba tokens keep their alpha: 0.16 → 0x29.
    assert.equal(t.selectionBackground, '#c69d6429');
    assert.equal(t.selectionInactiveBackground, '#c69d6429');
    const paper = m.buildXtermTheme(SKINS.paper);
    assert.equal(paper.background, '#efede7ff');
    assert.equal(paper.foreground, '#1e2227ff');
    assert.equal(paper.selectionBackground, '#8a6d3b1f');
    assert.equal(paper.cursor, '#2e7773ff');
    // Brass is the selection accent only: the cursor never takes it on any skin.
    for (const [id, skin] of Object.entries(SKINS)) {
        const cursor = m.buildXtermTheme(skin).cursor;
        assert.equal(cursor, `${skin.vars['--teal']}ff`, `${id} cursor is the resolved --teal`);
        assert.notEqual(cursor, `${skin.vars['--brass']}ff`, `${id} cursor is not the resolved --brass`);
    }
});

test('a light ground lifts program colours outside the ANSI table to 4.5:1; a dark ground leaves them (R05)', () => {
    const paper = m.xtermOptionsFor(SKINS.paper);
    assert.equal(paper.minimumContrastRatio, 4.5);
    assert.deepEqual(paper.theme, m.buildXtermTheme(SKINS.paper));
    for (const id of ['night', 'night-cool']) {
        assert.equal(m.xtermOptionsFor(SKINS[id]).minimumContrastRatio, 1, id);
    }
    // The placeholder snapshot is dark: no lift until the host says light.
    assert.equal(m.xtermOptionsFor({ scheme: 'dark', vars: {} }).minimumContrastRatio, 1);
});

test('a theme change rebuilds from the new snapshot; a token it lacks is left to xterm (R05)', () => {
    const night = m.buildXtermTheme(SKINS.night);
    const cool = m.buildXtermTheme(SKINS['night-cool']);
    assert.notEqual(night.background, cool.background);
    assert.equal(cool.background, '#131920ff');
    // The placeholder snapshot (no host theme yet) carries no vars: only the
    // palette goes in, never an empty or invented colour.
    const bare = m.buildXtermTheme({ scheme: 'dark', vars: {} });
    assert.deepEqual(Object.keys(bare).sort(), [...m.ANSI_KEYS].sort());
});

test('the ANSI palette is declared once per scheme and reads on every skin ground (R05)', () => {
    assert.deepEqual(Object.keys(m.ANSI_PALETTE).sort(), ['dark', 'light']);
    for (const scheme of ['dark', 'light']) {
        assert.deepEqual(Object.keys(m.ANSI_PALETTE[scheme]), [...m.ANSI_KEYS]);
    }
    for (const [id, skin] of Object.entries(SKINS)) {
        const t = m.buildXtermTheme(skin);
        const palette = m.ANSI_PALETTE[skin.scheme];
        for (const key of m.ANSI_KEYS) assert.equal(t[key], palette[key], `${id} ${key}`);
        const ground = skin.vars['--code-bg'];
        for (const key of m.ANSI_KEYS) {
            const ratio = contrast(palette[key], ground);
            // Dark black is a fill, not a text colour; the dimmest greys
            // (bright black, paper's bright white) are dim text at 3:1.
            if (skin.scheme === 'dark' && key === 'black') continue;
            const dim = key === 'brightBlack' || (skin.scheme === 'light' && key === 'brightWhite');
            const floor = dim ? 3 : 4.5;
            assert.ok(ratio >= floor, `${id}: ${key} ${palette[key]} on ${ground} is ${ratio.toFixed(2)}:1, under ${floor}`);
        }
    }
    // Paper's white is a readable grey: programs print white text expecting a dark ground.
    assert.ok(contrast(m.ANSI_PALETTE.light.white, SKINS.paper.vars['--code-bg']) >= 4.5);
});

test('no colour literal in the terminal outside the ANSI palette table (R05)', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const src = fs.readFileSync(path.join(process.cwd(), 'builtins', 'live', 'terminal', 'main.tsx'), 'utf8');
    const start = src.indexOf('export const ANSI_PALETTE');
    assert.ok(start > 0, 'the palette table exists');
    const end = src.indexOf('\n});\n', start);
    assert.ok(end > start, 'the palette table closes');
    const rest = src.slice(0, start) + src.slice(end);
    const literal = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|color-mix)\(/g;
    assert.deepEqual(rest.match(literal) ?? [], []);
    // Every var() names one of the 25 skin tokens, with no fallback after it.
    const NAMES = new Set([
        '--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2',
        '--ink', '--ink-2', '--ink-3', '--brass', '--brass-soft',
        '--teal', '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
        '--code-bg', '--prose-ink', '--shadow', '--lift',
        '--font-ui', '--font-mono', '--font-prose', '--radius',
    ]);
    assert.equal(NAMES.size, 25);
    for (const v of rest.match(/var\([^)]*\)/g) ?? []) {
        assert.match(v, /^var\(--[a-z0-9-]+\)$/, v);
        assert.ok(NAMES.has(v.slice(4, -1)), `${v} is not a skin token`);
    }
    // Every tokens.color.<key> read is one of the SDK's 19 colour keys (an
    // unknown key is undefined at runtime and paints nothing).
    const sdk = fs.readFileSync(path.join(process.cwd(), 'sdk', 'packages', 'renderer-hooks', 'src', 'index.tsx'), 'utf8');
    const block = sdk.slice(sdk.indexOf('export const tokens'));
    const colorBody = block.slice(block.indexOf('color: Object.freeze({'), block.indexOf('}),'));
    const COLOR_KEYS = new Set([...colorBody.matchAll(/^\s*(\w+):\s*'var\(/gm)].map((x) => x[1]));
    assert.equal(COLOR_KEYS.size, 19, `the token table carries 19 colour keys: ${[...COLOR_KEYS]}`);
    const reads = [...rest.matchAll(/tokens\.color\.(\w+)/g)].map((x) => x[1]);
    assert.ok(reads.length > 0, 'the chrome reads the colour tokens');
    for (const key of reads) assert.ok(COLOR_KEYS.has(key), `tokens.color.${key} is not a colour token`);
    // And every colour the builder hands xterm is a resolved token or a palette entry.
    const t = m.buildXtermTheme(SKINS.paper);
    const allowed = new Set<string>([
        ...Object.values(m.ANSI_PALETTE.light) as string[],
        '#efede7ff', '#1e2227ff', '#2e7773ff', '#8a6d3b1f',
    ]);
    for (const [key, value] of Object.entries(t)) assert.ok(allowed.has(value as string), `${key}=${value}`);
});

test('the header chrome is on skin tokens; the call to action is teal (R05)', () => {
    const cta = m.ctaStyle(false, false);
    assert.equal(cta.background, 'var(--teal-soft)');
    assert.equal(cta.border, '1px solid var(--teal)');
    assert.equal(cta.color, 'var(--teal)');
    const busy = m.ctaStyle(true, false);
    assert.equal(busy.background, 'var(--sheet-2)');
    assert.equal(busy.cursor, 'wait');
    assert.equal(m.ctaStyle(false, true).opacity, 0.6);
    assert.equal(m.statusColor('open'), 'var(--teal)');
    assert.equal(m.statusColor('busy'), 'var(--red)');
    assert.equal(m.statusColor('connecting'), 'var(--ink-2)');
    assert.equal(m.statusColor('ended'), 'var(--ink-3)');
    // Brass is the selection accent only: no chrome state takes it.
    for (const s of ['idle', 'connecting', 'reconnecting', 'open', 'busy', 'closed', 'ended']) {
        assert.doesNotMatch(m.statusColor(s), /brass/);
    }
    for (const style of [cta, busy]) assert.doesNotMatch(JSON.stringify(style), /brass|#/);
});
