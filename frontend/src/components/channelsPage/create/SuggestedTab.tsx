import type { Dispatch, SetStateAction } from 'react';
import type { CustomEntryDraft } from '@/lib/keystoreStructure';
import type { Account } from '@/lib/accountsApi';
import { isRunnerReady } from '@/lib/runnerReadiness';
import { btn, btnDanger, card, confirmStrip, sectionLabel } from '@/components/ui/formStyles';
import type { Suggestion } from '@/lib/api/suggested';
import { draftReadiness, suggestedDrafts } from './createBody';
import type { SuggestedState } from './state';
import { SuggestedQuickFields } from './SuggestedQuickFields';

interface Props {
    state: SuggestedState;
    update: Dispatch<SetStateAction<SuggestedState>>;
    accounts: Account[];
    busy: boolean;
    onOpenKeys: (draft?: CustomEntryDraft) => void;
}

/**
 * The Suggested tab: a suggestion from the catalog (the content installed
 * with the server), its explanation cells or not, and its
 * runner drafts, each set up through its quick fields or removed before the
 * channel can be created. Every other runner setting is edited later in the
 * channel's Capabilities.
 */
export function SuggestedTab({ state, update, accounts, busy, onOpenKeys }: Props) {
    const { catalog } = state;
    const suggestions = catalog?.suggestions ?? [];
    const suggestion = suggestions.find(item => item.id === state.pickedId);
    const pick = (id: string) => {
        const next = suggestions.find(item => item.id === id);
        update(prev => ({ ...prev, pickedId: id, drafts: next ? suggestedDrafts(next) : [], confirming: null }));
    };

    return <div className="flex flex-col gap-3">
        <div className={`flex gap-2 ${suggestions.length > 1 ? 'items-start' : 'items-center'}`}>
            {suggestions.length > 1
                ? <SuggestionCards suggestions={suggestions} pickedId={state.pickedId} disabled={busy} onPick={pick} />
                : <span className="min-w-0 flex-1 truncate text-[0.84375rem] font-medium text-ink">{suggestion?.name ?? (state.loading ? 'Loading suggestions…' : 'No suggestions')}</span>}
        </div>
        {suggestions.length === 1 && suggestion?.description && <p className="text-[0.78125rem] text-ink-2">{suggestion.description}</p>}
        {state.requestError && <p role="alert" className="text-[0.75rem] text-red">Could not read the suggestions: {state.requestError}</p>}
        {catalog?.error && <p role="alert" className="text-[0.75rem] text-red">Could not read the suggestions: {catalog.error}</p>}

        {suggestion && <>
            {suggestion.tutorial.length > 0 && <label className="flex items-center gap-2 text-[0.78125rem] text-ink-2">
                <input type="checkbox" className="accent-teal" checked={state.tutorial} disabled={busy} onChange={e => update(prev => ({ ...prev, tutorial: e.target.checked }))} />
                Include explanation cells
            </label>}
            <div className="flex flex-col gap-2 border-t border-line pt-3">
                <span className={sectionLabel}>Runners</span>
                <p className="text-[0.78125rem] text-ink-2">Set up each runner here, or remove the ones you don't need. Edit them later in Capabilities.</p>
                <button type="button" className={`${btn} self-start`} disabled={busy} onClick={() => onOpenKeys()}>Open Key manager</button>
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

/** The catalog's suggestions as cards side by side (Standard, then Basic
 *  and any other, in catalog order), one picked: each names its setup and
 *  says what a channel made from it holds. */
function SuggestionCards({ suggestions, pickedId, disabled, onPick }: {
    suggestions: Suggestion[]; pickedId: string; disabled: boolean; onPick: (id: string) => void;
}) {
    return <div role="radiogroup" aria-label="Suggestion" className="grid min-w-0 flex-1 grid-cols-1 gap-2 sm:grid-cols-2">
        {suggestions.map(item => {
            const picked = item.id === pickedId;
            return <button key={item.id} type="button" role="radio" aria-checked={picked} disabled={disabled}
                data-suggestion={item.id} onClick={() => onPick(item.id)}
                className={`${card} flex flex-col gap-1 p-3 text-left ${picked ? 'border-teal ring-1 ring-teal' : 'hover:bg-sheet-2'}`}>
                <span className="text-[0.84375rem] font-medium text-ink">{item.name}</span>
                {'saved' in item && <span className="text-[0.6875rem] text-ink-3">Saved suggestion</span>}
                {item.description && <span className="text-[0.75rem] text-ink-2">{item.description}</span>}
            </button>;
        })}
    </div>;
}
