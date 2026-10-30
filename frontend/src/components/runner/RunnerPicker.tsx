import { useMemo, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useRunnerCatalog } from '@/stores/useRunnerStore';
import { cellHasLiveRun } from '@/lib/runningCell';
import type { RunnerEntry } from '@/lib/runnerApi';
import { runRunner } from '@/lib/runAttach';
import { selectedFileOf } from '@/lib/selectedFile';
import { lastRunnerOf } from '@/lib/runnerChoice';
import { PlayGlyph } from '@/components/ui/PlayGlyph';
import { CellPanel, type CellPanelHost } from '@/components/cellPanel/CellPanel';

/** Lower-cased, whitespace-collapsed words of `text`. */
const words = (text: string): string[] => text.toLowerCase().split(/\s+/).filter(Boolean);

/** The runners a search keeps, best match first. The filter is a token AND
 *  over name + description + tags; the ranking puts runners whose NAME
 *  carries every token first — an exact name, then a name that starts with
 *  the search, then a name that merely contains every token — and the ones
 *  matched only through their description or tags after. Ties keep the
 *  catalog's order. (Typing "Chat agent" must put Chat agent, not an
 *  Annotation agent whose description mentions chat, on top: Enter runs the
 *  top row.) */
function searchRunners(catalog: readonly RunnerEntry[], query: string): RunnerEntry[] {
    const tokens = words(query);
    const kept = catalog.filter(r =>
        tokens.every(token => `${r.name} ${r.description} ${r.tags.join(' ')}`.toLowerCase().includes(token)));
    if (tokens.length === 0) return kept;
    const phrase = tokens.join(' ');
    const rank = (r: RunnerEntry): number => {
        const name = words(r.name).join(' ');
        if (name === phrase) return 0;
        if (name.startsWith(phrase)) return 1;
        if (tokens.every(token => name.includes(token))) return 2;
        return 3;
    };
    return kept
        .map((r, at) => ({ r, at, rank: rank(r) }))
        .sort((a, b) => a.rank - b.rank || a.at - b.at)
        .map(({ r }) => r);
}

/** The row note of a runner that takes the cell's selected file. */
const selectedFileNote = (path: string | null): string =>
    path ? `will use ${path.slice(path.lastIndexOf('/') + 1)}` : 'no file selected';

/** The note at the top of the panel: runners share the cell's files. */
const COLLISION_NOTE = 'Several runners in one cell can collide on the files they work on. Run in a new cell to keep them apart.';

/**
 * The runners panel (row P02): the cell's panel of kind `runners`. It runs:
 * one runner is selected at a time (the cell's last runner when it opens);
 * the arrows, a click and a search move the selection. The footer's primary
 * (↵ or a double-click) runs the selected runner in this cell, and "in new
 * cell" (Shift+↵) runs it in a clone of this cell to the right instead
 * (D-P02a). Runners are made and edited in the Capabilities manage view; an
 * empty catalog offers the way there.
 *
 * A runner that takes the cell's selected file (the active file tab, else
 * the Files pane's file selection) says which one its row will use, and a
 * run in this cell or a new one hands it that path.
 */
export function RunnerPicker({ cellId, host }: { cellId: string; host?: CellPanelHost }) {
    const catalog = useRunnerCatalog();
    const query = useCollimateStore(s => {
        const session = s.pickSessions[cellId];
        return session?.kind === 'runners' ? session.query : '';
    });
    const update = useCollimateStore(s => s.updatePickSession);
    const closeSession = useCollimateStore(s => s.closePickSession);
    const cellBusy = useCollimateStore(s => cellHasLiveRun(s, cellId));
    const selectedFile = useCollimateStore(s => selectedFileOf(s.cellLayouts[cellId]));
    const requestManage = useCollimateStore(s => s.requestCapabilitiesManage);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [selectedId, setSelectedId] = useState<string | null>(() => lastRunnerOf(useCollimateStore.getState(), cellId));
    const rows = useMemo(() => searchRunners(catalog.data ?? [], query), [catalog.data, query]);
    // The selection is an id, so a refresh that reorders or drops rows never
    // leaves the panel pointing at a different runner; an id the list lacks
    // falls back to the top row.
    const current = rows.find(row => row.id === selectedId) ?? rows[0];

    const run = async (runner: RunnerEntry, newCell: boolean) => {
        setBusy(true); setError('');
        try {
            const file = runner.takes_selected_file ? selectedFile : null;
            await runRunner({
                runner: runner.id, runnerRevision: runner.revision, cellId,
                ...(newCell ? { newCell: true } : {}), ...(file ? { selectedFile: file } : {}),
            });
            closeSession(cellId, 'runners');
        } catch (e) { setError(e instanceof Error ? e.message : 'Could not start runner'); }
        finally { setBusy(false); }
    };

    const runBlocked = !current ? 'No runner selected' : current.invalid ? current.invalid.reason : undefined;
    const disabled = busy || !!runBlocked;
    const noRunners = !catalog.busy && !(catalog.data ?? []).length;

    return <CellPanel
        cellId={cellId}
        kind="runners"
        host={host}
        title="Runners"
        note={<span className={cellBusy ? 'text-teal' : undefined}>{COLLISION_NOTE}</span>}
        query={query}
        onQuery={next => {
            update(cellId, 'runners', { query: next });
            // A new search selects its best match.
            setSelectedId(null);
        }}
        placeholder="Search runners"
        error={error || catalog.error || undefined}
        rows={rows.map(runner => ({
            id: runner.id, name: runner.name, description: runner.description, tags: runner.tags,
            reason: runner.invalid?.reason,
            note: runner.takes_selected_file ? selectedFileNote(selectedFile) : undefined,
        }))}
        selectedId={current?.id ?? null}
        onSelect={setSelectedId}
        empty={catalog.busy ? 'Loading runners…' : noRunners ? 'No runners yet. Make one in Capabilities.' : 'No runner matches'}
        emptyAction={noRunners ? { label: 'Open Capabilities', onSelect: () => {
            requestManage({ kind: 'runner', runner: { action: 'add' } });
            closeSession(cellId, 'runners');
        } } : undefined}
        primary={{ label: current ? `Run ‘${current.name}’` : 'Run', icon: PlayGlyph, intent: 'run', reason: runBlocked, disabled, onRun: () => { if (current) void run(current, false); } }}
        secondary={{ label: 'in new cell', hint: '⇧↵', disabled, onRun: () => { if (current) void run(current, true); } }}
        onClose={() => closeSession(cellId, 'runners')}
    />;
}
