import { useEffect, useRef, type Dispatch, type SetStateAction } from 'react';
import { RefreshCw } from 'lucide-react';
import type { Account } from '@/lib/accountsApi';
import { localFolderPicker, serverIsRemote } from '@/lib/desktop';
import { isRunnerReady } from '@/lib/runnerReadiness';
import { btn, btnDanger, card, confirmStrip, formInput, formLabel, formSelect, sectionLabel } from '@/components/ui/formStyles';
import { draftReadiness, suggestedDrafts } from './createBody';
import type { SuggestedState } from './state';
import { SuggestedQuickFields } from './SuggestedQuickFields';

/** How long Refresh is held before it offers a folder instead. */
const HOLD_MS = 5000;

interface Props {
    state: SuggestedState;
    update: Dispatch<SetStateAction<SuggestedState>>;
    accounts: Account[];
    busy: boolean;
    /** Fetch the latest release's content (a folder is read again). */
    onRefresh: () => void;
    /** Read the catalog from a folder on the server, or (null) stop reading one. */
    onSource: (path: string | null) => void;
    onOpenKeys: () => void;
}

/**
 * The Suggested tab: a suggestion from the catalog (the content installed
 * with the app, the latest release's content once Refresh fetched it, or a
 * folder standing in for them), its explanation cells or not, and its
 * runner drafts, each set up through its quick fields or removed before the
 * channel can be created. Every other runner setting is edited later in the
 * channel's Capabilities.
 */
export function SuggestedTab({ state, update, accounts, busy, onRefresh, onSource, onOpenKeys }: Props) {
    const { catalog } = state;
    const suggestions = catalog?.suggestions ?? [];
    const suggestion = suggestions.find(item => item.id === state.pickedId);
    const folderLabel = serverIsRemote() ? 'Folder on the server' : 'Local folder';
    const chooseFolder = localFolderPicker();
    const source = catalog?.source;
    const folder = source?.kind === 'folder';
    const fetchedAt = source?.fetched_at ? new Date(source.fetched_at).toLocaleString() : '';
    // Where the suggestions come from, while nothing failed (a folder says so in its row).
    const sourceNote = source?.kind === 'bundled'
        ? 'Installed with the app. Refresh fetches the suggestions of the latest release for new channels.'
        : source?.kind === 'github' && fetchedAt ? `From the latest release on GitHub, fetched at ${fetchedAt}.` : '';

    const pick = (id: string) => {
        const next = suggestions.find(item => item.id === id);
        update(prev => ({ ...prev, pickedId: id, drafts: next ? suggestedDrafts(next) : [], confirming: null }));
    };
    // A hold toggles the folder options: it opens them, and held again it
    // returns the page to normal mode, which also stops reading the folder
    // when one is in use (a folder source keeps the row shown).
    const toggleFolder = () => {
        if (!state.folderOpen && !folder) { update(prev => ({ ...prev, folderOpen: true })); return; }
        update(prev => ({ ...prev, folderOpen: false, folderError: '' }));
        if (folder) onSource(null);
    };

    return <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
            {suggestions.length > 1
                ? <select aria-label="Suggestion" className={`${formSelect} flex-1`} value={state.pickedId} disabled={busy} onChange={e => pick(e.target.value)}>
                    {suggestions.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
                : <span className="min-w-0 flex-1 truncate text-[0.84375rem] font-medium text-ink">{suggestion?.name ?? (state.loading ? 'Loading suggestions…' : 'No suggestions')}</span>}
            <HoldRefresh disabled={busy || state.loading || state.folderBusy} onRefresh={onRefresh} onHold={toggleFolder}
                title={folder ? 'Read the folder again' : 'Fetch the suggestions of the latest release from GitHub for new channels'} />
        </div>
        {suggestion?.description && <p className="text-[0.78125rem] text-ink-2">{suggestion.description}</p>}
        {sourceNote && !catalog?.error && <p className="text-[0.71875rem] text-ink-3">{sourceNote}</p>}
        {state.requestError && <p role="alert" className="text-[0.75rem] text-red">Could not read the suggestions: {state.requestError}</p>}
        {catalog?.error && <p role="alert" className="text-[0.75rem] text-red">
            {!suggestions.length ? `Could not read the suggestions: ${catalog.error}`
                : catalog.source.kind === 'github' && fetchedAt ? `Showing the copy fetched at ${fetchedAt}: ${catalog.error}`
                    : `Showing the suggestions installed with the app: ${catalog.error}`}
        </p>}

        {(state.folderOpen || folder) && <div className={`${card} flex flex-col gap-2 p-3`}>
            {folder && <p className="break-all text-[0.75rem] text-ink-2">Reading the suggestions from {source.path}.</p>}
            <label className="flex flex-col gap-1"><span className={formLabel}>{folderLabel}</span>
                <div className="flex gap-2">
                    <input aria-label={folderLabel} className={formInput} placeholder="/absolute/path/to/folder" value={state.folderPath} disabled={state.folderBusy}
                        onChange={e => update(prev => ({ ...prev, folderPath: e.target.value }))} />
                    {chooseFolder && <button type="button" className={btn} disabled={state.folderBusy}
                        onClick={() => void chooseFolder().then(path => { if (path) update(prev => ({ ...prev, folderPath: path })); })
                            .catch(e => update(prev => ({ ...prev, folderError: e instanceof Error ? e.message : String(e) })))}>Choose folder…</button>}
                </div>
            </label>
            <span className="text-[0.71875rem] text-ink-3">A folder laid out like the repository's content/ folder, with suggested/, generators/ and renderers/ folders.</span>
            <div className="flex gap-2">
                <button type="button" className={btn} disabled={state.folderBusy || !state.folderPath.trim()} onClick={() => onSource(state.folderPath.trim())}>Use folder</button>
                <button type="button" className={btn} disabled={state.folderBusy || !folder} onClick={() => onSource(null)}>Stop using folder</button>
            </div>
            {state.folderError && <p role="alert" className="text-[0.75rem] text-red">{state.folderError}</p>}
        </div>}

        {suggestion && <>
            {suggestion.tutorial.length > 0 && <label className="flex items-center gap-2 text-[0.78125rem] text-ink-2">
                <input type="checkbox" className="accent-teal" checked={state.tutorial} disabled={busy} onChange={e => update(prev => ({ ...prev, tutorial: e.target.checked }))} />
                Include explanation cells
            </label>}
            <div className="flex flex-col gap-2 border-t border-line pt-3">
                <span className={sectionLabel}>Runners</span>
                <p className="text-[0.78125rem] text-ink-2">Set up each runner here, or remove the ones you don't need. Edit them later in Capabilities.</p>
                <button type="button" className={`${btn} self-start`} disabled={busy} onClick={onOpenKeys}>Open Key manager</button>
            </div>
            <div className="flex flex-col gap-2">
                    {state.drafts.map(({ draft, quick_fields }, index) => {
                        const readiness = draftReadiness(draft, suggestion, accounts);
                        const ready = isRunnerReady(readiness);
                        const generator = suggestion.runner_generators.find(item => item.id === draft.runner.generator);
                        return <div key={draft.runner.id} className={`${card} overflow-hidden`} data-runner-draft={draft.runner.id}>
                            <div className="flex items-center gap-3 p-3">
                                <div className="min-w-0 flex-1">
                                    <span className="block text-[0.8125rem] font-medium text-ink">{draft.runner.name}</span>
                                    {draft.runner.description && <span className="block text-[0.71875rem] text-ink-2">{draft.runner.description}</span>}
                                    {ready ? <span className="block text-[0.71875rem] text-green">Ready</span>
                                        : <span className="block text-[0.71875rem] text-red">Needs setup: {[...readiness.missing, ...readiness.invalid].join(', ')}</span>}
                                </div>
                                <button type="button" className={btn} disabled={busy} onClick={() => update(prev => ({ ...prev, confirming: index }))}>Delete</button>
                            </div>
                            {generator && <SuggestedQuickFields draft={draft} generator={generator} fields={quick_fields}
                                accounts={accounts} disabled={busy} onOpenKeys={onOpenKeys}
                                onChange={next => update(prev => ({ ...prev, drafts: prev.drafts.map(item =>
                                    item.draft.runner.id === draft.runner.id ? { ...item, draft: next } : item) }))} />}
                            {state.confirming === index && <div className={confirmStrip}>
                                <span className="min-w-0 flex-1">Delete “{draft.runner.name}”?</span>
                                <button type="button" className={btn} onClick={() => update(prev => ({ ...prev, confirming: null }))}>Keep</button>
                                <button type="button" className={btnDanger} onClick={() => update(prev => ({ ...prev, drafts: prev.drafts.filter((_, i) => i !== index), confirming: null }))}>Delete</button>
                            </div>}
                        </div>;
                    })}
                    {!state.drafts.length && <p className="text-[0.75rem] text-ink-3">No runners: add them later in Capabilities.</p>}
                </div>
        </>}
    </div>;
}

/** Refresh, which held for HOLD_MS toggles the folder options instead: the press
 *  that ends a completed hold refreshes nothing. The hold shows nothing
 *  while it runs and the button does not mention it. A pointer press clicks
 *  as usual; Enter and Space hold the same way, and a shorter key press
 *  refreshes when the key comes up (neither key clicks the button itself,
 *  so a press the button never sees end cannot leave a hold running). */
function HoldRefresh({ disabled, onRefresh, onHold, title }: { disabled: boolean; onRefresh: () => void; onHold: () => void; title: string }) {
    const timer = useRef<number | null>(null);
    /** The hold ran its course: the press ending it does not refresh. */
    const fired = useRef(false);
    /** The key whose press began on this button, if one is down. */
    const key = useRef<string | null>(null);
    const clear = () => {
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = null;
    };
    /** Drop the press altogether: focus left, or the button went disabled. */
    const cancel = () => { clear(); key.current = null; fired.current = false; };
    const start = () => {
        clear();
        fired.current = false;
        timer.current = window.setTimeout(() => {
            timer.current = null;
            fired.current = true;
            onHold();
        }, HOLD_MS);
    };
    useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
    // A disabled button hears no release: a press under way ends there.
    useEffect(() => {
        if (!disabled) return;
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = null;
        key.current = null;
        fired.current = false;
    }, [disabled]);
    const pointerStop = () => { if (key.current === null) clear(); };
    return <button type="button" className={`${btn} flex-none`} disabled={disabled} title={title}
            onPointerDown={e => { if (e.button > 0 || key.current !== null) return; start(); }}
            onPointerUp={pointerStop} onPointerLeave={pointerStop} onPointerCancel={pointerStop}
            onKeyDown={e => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                // Enter would click on keydown and Space on keyup: the keyup
                // below decides instead, once per press.
                e.preventDefault();
                if (e.repeat || key.current !== null) return;
                key.current = e.key;
                start();
            }}
            onKeyUp={e => {
                if (e.key !== key.current) return;
                e.preventDefault();
                const held = fired.current;
                key.current = null;
                fired.current = false;
                clear();
                if (!held) onRefresh();
            }}
            onBlur={cancel}
            onClick={() => {
                if (fired.current) { fired.current = false; return; }
                onRefresh();
            }}><RefreshCw className="size-[0.8125rem]" />Refresh</button>;
}
