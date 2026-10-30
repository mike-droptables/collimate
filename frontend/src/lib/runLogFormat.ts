/**
 * Canonical run-log line formatter — the TS half of a contract shared
 * byte-for-byte with the backend `api/process_manager/log_format.py`.
 *
 * The live drawer renders every run_event through `formatLogLine`, and the
 * persisted run log (run_artifacts `logs.txt`) is the same rows formatted the same way,
 * so live === saved. If you change a tag, the timestamp slice, the
 * `[LEVEL]`-stderr re-tag, or the allow-list here, change it in log_format.py
 * too, and in the golden vectors both suites read
 * (api/tests/fixtures/run-log-vectors.json).
 *
 * Each run_event → one line:  `[HH:MM:SS] TAG    text`  (LOCAL wall-clock)
 *
 * `gen.log()` is NOT a distinct event type — it is `[LEVEL]`-prefixed stderr
 * (the SDK wires the generator logger with `"[%(levelname)s] %(message)s"`),
 * so stderr rows that open with a known level frame are re-tagged
 * (INFO/DEBUG→LOG, WARNING→WARN, ERROR/CRITICAL→ERR) and the frame stripped.
 * Bare stderr is tagged STDERR, deliberately NOT ERR: half the tools a
 * generator shells out to (pip, npm, git, curl) narrate normal progress on
 * stderr, and painting every installer line as an error made real failures
 * invisible. ERR is reserved for lines a logger explicitly leveled ERROR.
 *
 * `run_started` / `run_finished` are host-fabricated bookends (tag RUN), not
 * real run_events rows: the client seeds/appends them around the live feed
 * and finalization bakes the same lines into logs.txt — they name the runner
 * that ran and the final status.
 */

const TAG_WIDTH = 6;

const LEVEL_TAGS: Record<string, string> = {
    INFO: 'LOG',
    DEBUG: 'LOG',
    WARNING: 'WARN',
    ERROR: 'ERR',
    CRITICAL: 'ERR',
};

/** `HH:MM:SS` in LOCAL time from a SQLite `CURRENT_TIMESTAMP` string
 *  (`YYYY-MM-DD HH:MM:SS`, UTC, no offset) or an ISO string. The offset-less
 *  string is explicitly pinned to UTC before conversion — a bare
 *  `new Date(s)` would read it as local and skew it. Python's side converts
 *  the same way (UTC → server-local); the server runs on the user's machine,
 *  so both halves render the same wall clock. Unparseable → raw slice. */
function hhmmss(createdAt: unknown): string {
    const s = String(createdAt ?? '');
    if (s.length >= 19 && (s[10] === ' ' || s[10] === 'T')) {
        const d = new Date(s.slice(0, 19).replace(' ', 'T') + 'Z');
        if (!Number.isNaN(d.getTime())) {
            const p = (n: number) => String(n).padStart(2, '0');
            return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
        }
        return s.slice(11, 19);
    }
    return '';
}

function asDict(payload: unknown): Record<string, unknown> {
    try {
        const v = JSON.parse(String(payload));
        return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
    } catch {
        return {};
    }
}

function rstripNewlines(text: string): string {
    return text.replace(/\n+$/, '');
}

/** If `text` opens with a `[LEVEL] ` frame, return `[tag, stripped]`, else `[null, text]`. */
function stripLevel(text: string): [string | null, string] {
    if (text.startsWith('[')) {
        const close = text.indexOf('] ');
        if (close !== -1) {
            const tag = LEVEL_TAGS[text.slice(1, close)];
            if (tag) return [tag, text.slice(close + 2)];
        }
    }
    return [null, text];
}

/** The allow-list is this if-chain: keep the handled types identical to
 *  `_summarize` in log_format.py. Anything else → tag null (dropped). */
function summarize(eventType: string, payload: unknown): { tag: string | null; text: string } {
    const raw = payload == null ? '' : String(payload);
    if (eventType === 'stdout') return { tag: 'OUT', text: rstripNewlines(raw) };
    if (eventType === 'stderr') {
        // Bare stderr = subprocess narration, not an error (see header).
        const [tag, body] = stripLevel(rstripNewlines(raw));
        return { tag: tag ?? 'STDERR', text: body };
    }
    if (eventType === 'log') return { tag: 'LOG', text: rstripNewlines(raw) };
    if (eventType === 'ipc_error') return { tag: 'IPCERR', text: rstripNewlines(raw) };
    if (eventType === 'timeout') return { tag: 'TIMOUT', text: rstripNewlines(raw) };

    const d = asDict(payload);
    if (eventType === 'phase') {
        const p = String(d.phase || '');
        const m = String(d.message || '');
        return { tag: 'PHASE', text: m ? `${p} — ${m}` : p };
    }
    if (eventType === 'progress') return { tag: 'PROG', text: `${d.current ?? '?'}/${d.total ?? '?'}` };
    if (eventType === 'finish') {
        const err = String(d.error_message || '');
        // An explicit null is falsy, as in log_format.py and the run status.
        const base = ('success' in d ? d.success : true) ? 'ok' : 'failed';
        return { tag: 'DONE', text: err ? `${base}: ${err}` : base };
    }
    if (eventType === 'stop') return { tag: 'STOP', text: String(d.stop_type || 'stopped') };
    if (eventType === 'commit') return { tag: 'COMMIT', text: String(d.name || '') };
    if (eventType === 'egress_denied') {
        const host = String(d.host || '');
        const method = String(d.method || '');
        return { tag: 'EGRESS', text: `blocked: ${method} ${host}:${d.port ?? ''}`.replace(/[:\s]+$/, '') };
    }
    // The bookends name the runner; a reload re-attach may not know it, in
    // which case the segment just drops out.
    if (eventType === 'run_started') {
        return { tag: 'RUN', text: ['started', String(d.runner || '')].filter(Boolean).join(' · ') };
    }
    if (eventType === 'run_finished') {
        return { tag: 'RUN', text: ['finished', String(d.status || ''), String(d.runner || '')].filter(Boolean).join(' · ') };
    }
    return { tag: null, text: '' };
}

/** Format one run_event into the canonical line, or `null` if its type is not
 *  part of the user-facing log (allow-list miss). */
export function formatLogLine(eventType: string, payload: unknown, createdAt?: unknown): string | null {
    const { tag, text } = summarize(eventType, payload);
    if (tag === null) return null;
    const ts = hhmmss(createdAt);
    const prefix = ts ? `[${ts}] ` : '';
    return `${prefix}${tag.padEnd(TAG_WIDTH)} ${text}`;
}
