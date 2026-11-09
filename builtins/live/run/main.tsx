/**
 * Run — the run card renderer (binds `.run`).
 *
 * The host writes one card per run, `_runs/<label>/<label>.run`, before the
 * run's process starts, and removes it when the run ends: it is the run's
 * first locked file, so a run always has a tab to select even before (or
 * without) a surface of its own. It is JSON:
 *
 *   {"v": 1, "run_id", "runner": {"id", "name"}, "generator", "label",
 *    "folder", "started_at" (ISO 8601 UTC), "git"?}
 *
 * `git` is there only on a cell with repositories: `{"repositories": [<path>,
 * …], "access": "holder"}` when this run holds their `.git` (one live run
 * per cell does); `"access": "readonly"` when another run holds it and this
 * one has a read-only `.git` that follows the cell; `"access": "none"` for
 * a `docker: limited` run or a machine without usable git. The last two
 * carry the host's `"reason"` sentence. A path is cell-relative, `.` for
 * the cell root.
 *
 * This renderer only shows it — who is running, since when, and whether it
 * has the cell's git — and says
 * where the rest of the run is: its Stop is under its row in the status
 * section the status pill at the cell's top left opens, and its lines are
 * under Log in the
 * annotation pane (the row's message icon opens the run there). The run's other UI files open as the cell's file tabs, not
 * here.
 *
 * Rendered by the host's NativeRenderer (shadow DOM): inline styles only, on
 * the skin tokens.
 */
import React, { useMemo } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile, useTheme } from '/api/sdk/react.js';

// ── the card ──────────────────────────────────────────────────────────

export type RunCard = {
    runId: string;
    runnerId: string;
    runnerName: string;
    generator: string;
    label: string;
    folder: string;
    startedAt: string;
    git: RunGit | null;
};

/** Whether the run holds its cell's git, has it read-only or has none;
 *  null on a cell without repositories. */
export type GitAccess = 'holder' | 'readonly' | 'none';
export type RunGit = { repositories: string[]; access: GitAccess; reason: string };

const ACCESS: readonly GitAccess[] = ['holder', 'readonly', 'none'];

const asStr = (v: unknown): string => (typeof v === 'string' ? v.trim() : '');
const asObj = (v: unknown): any => (v && typeof v === 'object' && !Array.isArray(v) ? v : null);

/** The card, or null for anything the host did not write (unparseable, a
 *  version this renderer does not know, no run id or label). */
export function parseRunCard(raw: string): RunCard | null {
    let doc: any;
    try { doc = JSON.parse((raw || '').trim()); } catch { return null; }
    const j = asObj(doc);
    if (!j || j.v !== 1) return null;
    const runId = asStr(j.run_id);
    const label = asStr(j.label);
    if (!runId || !label) return null;
    const runner = asObj(j.runner) || {};
    return {
        runId,
        runnerId: asStr(runner.id),
        runnerName: asStr(runner.name),
        generator: asStr(j.generator),
        label,
        folder: asStr(j.folder),
        startedAt: asStr(j.started_at),
        git: parseGit(j.git),
    };
}

/** The card's `git`, or null when the cell has no repositories (or the
 *  field is not the host's shape). */
function parseGit(v: unknown): RunGit | null {
    const g = asObj(v);
    if (!g || !ACCESS.includes(g.access) || !Array.isArray(g.repositories)) return null;
    const repositories = g.repositories.filter((p: unknown): p is string => typeof p === 'string');
    if (repositories.length === 0) return null;
    return { repositories, access: g.access, reason: asStr(g.reason) };
}

/** The card's Git row: the repositories (the root one as the cell root),
 *  then that this run holds the `.git`, or the host's own sentence for a
 *  read-only `.git` (which run holds the git) or for none (why). */
export function gitText(git: RunGit): string {
    const paths = git.repositories.map((p) => (p === '.' ? 'cell root' : p)).join(', ');
    if (git.access === 'holder') return `${paths} — this run holds the .git`;
    return `${paths} — ${git.reason}`;
}

/** The card's title: the runner's name, else its id. The host always writes a
 *  runner id (composition sub-runs get no card), so the generator, then the
 *  label, is only a fallback for a card missing both. */
export function runTitle(card: RunCard): string {
    return card.runnerName || card.runnerId || card.generator || card.label;
}

const pad = (n: number): string => String(n).padStart(2, '0');

/** `YYYY-MM-DD HH:MM:SS` in the viewer's local time, or '' for a timestamp
 *  that does not parse. */
export function formatStarted(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
        + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Where the run's Stop and log are, said once, under the card. */
export const RUN_NOTE = "This run's Stop is under its row in the cell's status section, which the status pill at the cell's top left opens. The row's message icon opens the run in the annotation pane, whose Log option shows its lines.";

// ── the view ──────────────────────────────────────────────────────────
//
// Every colour and face is a skin token (`var(--…)`, inherited into the shadow
// root), so the card follows a skin change on its own. The card carries no
// live state of its own (history and Logs show it after its run ends), so its
// tag is neutral; it never marks a selection, so it carries no brass.

const C = tokens.color;

/** The card's styles, all on skin tokens. */
export const STYLES: Record<string, React.CSSProperties> = {
    root: {
        width: '100%', height: '100%', boxSizing: 'border-box', overflow: 'auto',
        background: C.sheet, color: C.ink, padding: 16,
        fontFamily: tokens.font.ui, fontSize: 13,
    },
    empty: { color: C.ink3 },
    head: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
    title: { fontSize: 14.5, fontWeight: 600, color: C.ink },
    tag: {
        display: 'inline-flex', alignItems: 'center', boxSizing: 'border-box', height: 20, padding: '0 8px',
        borderRadius: 999, border: `1px solid ${C.line2}`, color: C.ink2,
        fontSize: 11.5, fontWeight: 500,
    },
    label: { fontFamily: tokens.font.mono, fontSize: 11.5, color: C.ink3 },
    panel: {
        marginTop: 6, padding: '4px 12px', borderRadius: `calc(${tokens.radius} - 2px)`,
        background: C.sheet2, border: `1px solid ${C.line}`,
    },
    row: { display: 'flex', gap: 12, padding: '4px 0', minWidth: 0, alignItems: 'baseline' },
    rowName: { width: 96, flex: 'none', color: C.ink2, fontSize: 12 },
    rowValue: { flex: 1, minWidth: 0, fontSize: 12.5, color: C.ink, overflowWrap: 'anywhere' },
    rowMono: { fontFamily: tokens.font.mono, fontSize: 11.5 },
    note: { marginTop: 16, fontSize: 12, color: C.ink3 },
};

function Row({ name, value, mono }: { name: string; value: string; mono?: boolean }) {
    return (
        <div style={STYLES.row}>
            <div style={STYLES.rowName}>{name}</div>
            <div style={mono ? { ...STYLES.rowValue, ...STYLES.rowMono } : STYLES.rowValue}>
                {value || '—'}
            </div>
        </div>
    );
}

function RunCardView() {
    const { content, api } = useCollimateFile();
    // The styles are var() references and repaint without React; the
    // subscription keeps the skin id on the root current for a skin change.
    const { themeId } = useTheme(api);
    const card = useMemo(() => parseRunCard(content), [content]);
    if (!card) {
        return (
            <div data-skin={themeId} style={{
                ...STYLES.root, display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
                <span style={STYLES.empty}>This .run file is not a run card.</span>
            </div>
        );
    }
    return (
        <div style={STYLES.root} data-testid="run-card" data-skin={themeId}>
            <div style={STYLES.head}>
                <span style={STYLES.title}>{runTitle(card)}</span>
                <span style={STYLES.tag} data-testid="run-card-tag">run</span>
                <span style={STYLES.label}>{card.label}</span>
            </div>
            <div style={{ ...STYLES.panel, marginTop: 12 }}>
                <Row name="Runner" value={card.runnerName && card.runnerId
                    ? `${card.runnerName} (${card.runnerId})` : card.runnerId} />
                <Row name="Generator" value={card.generator} mono />
                <Row name="Label" value={card.label} mono />
                <Row name="Started" value={formatStarted(card.startedAt)} />
                <Row name="Folder" value={card.folder} mono />
                {card.git && <Row name="Git" value={gitText(card.git)} />}
            </div>
            <div style={STYLES.note} data-testid="run-card-note">{RUN_NOTE}</div>
        </div>
    );
}

export default defineRenderer({ component: RunCardView });
