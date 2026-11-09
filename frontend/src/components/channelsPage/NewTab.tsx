import { useCallback, useEffect, useRef, useState } from 'react';
import type { CreateChannelBody } from '@/lib/api/channels';
import { fetchSuggested, type SuggestedCatalog } from '@/lib/api/suggested';
import { withSavedSuggestions } from '@/lib/api/userCatalog';
import { fetchAccounts, type Account } from '@/lib/accountsApi';
import { btn, btnPrimary, formInput, formLabel, tabOff, tabOn } from '@/components/ui/formStyles';
import type { CustomEntryDraft } from '@/lib/keystoreStructure';
import { ConfigTab } from './create/ConfigTab';
import { SuggestedTab } from './create/SuggestedTab';
import { configBlockers, configSelection, suggestedBlockers, suggestedDrafts } from './create/createBody';
import { initialConfig, initialSuggested, initialTab, type ChannelChoice, type CreateTab, type SuggestedState } from './create/state';

const TABS: { id: CreateTab; label: string }[] = [
    { id: 'suggested', label: 'Suggested' },
    { id: 'config', label: 'Config' },
];

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** The catalog answered: the first suggestion is picked, with fresh drafts. */
function withCatalog(prev: SuggestedState, catalog: SuggestedCatalog): SuggestedState {
    const picked = catalog.suggestions[0];
    return {
        ...prev, loading: false, catalog, requestError: '',
        pickedId: picked?.id ?? '',
        drafts: picked ? suggestedDrafts(picked) : [],
        confirming: null,
    };
}

interface Props {
    /** Rejects on failure, so the view shows why and re-enables Create. */
    onCreate: (name: string, body: CreateChannelBody) => Promise<void> | void;
    /** Back to the open channel; absent when there is none. */
    onCancel?: () => void;
    /** The channels to copy from. */
    channels: ChannelChoice[];
    /** The channel open when the view was asked for: the Config tab's
     *  default source. */
    sourceChannelId: string | null;
    /** The page shows this tab: the Suggested catalog is read the first
     *  time it shows here. */
    shown?: boolean;
    /** The default location's path, where every new channel goes (the user
     *  is never asked). Null until the page knows it: the line is left out.
     *  P8 passes the default location of `GET /api/storage/locations`. */
    savedIn?: string | null;
    /** Open Accounts on this machine (the page's docked settings panel);
     *  with a draft, on a new custom entry shaped like a runner's role. */
    onOpenKeys: (draft?: CustomEntryDraft) => void;
    /** Changes with each account change made in that panel (it docks
     *  beside this tab, which stays in view): the accounts the drafts are
     *  checked against are read again. */
    accountsRevision: number;
}

/**
 * The channels page's New tab (formerly the new-channel page): two tabs,
 * with the channel name beside Create in the footer. Suggested starts from the SDK's suggested catalog; Config
 * assembles the channel from existing channels and files. It opens on Config
 * when channels exist to copy from,
 * else on Suggested. Every tab's state lives here, and both tab bodies stay
 * mounted (the inactive one hidden), so switching tabs keeps the work and
 * the fetches under way. Accounts open in the page's settings panel beside
 * it, so a draft stays in view while its account is made.
 */
export function NewTab({ onCreate, onCancel, channels, sourceChannelId, shown = true, savedIn = null, onOpenKeys, accountsRevision }: Props) {
    const [name, setName] = useState('');
    const [tab, setTab] = useState<CreateTab>(() => initialTab(channels));
    const [suggested, setSuggested] = useState(initialSuggested);
    const [config, setConfig] = useState(() => initialConfig(channels, sourceChannelId));
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const refreshAccounts = useCallback(() => {
        void fetchAccounts().then(data => setAccounts(data.accounts), e => setError(message(e, 'Could not read the accounts.')));
    }, []);
    // The Suggested tab reads the catalog (the app's suggestions, then the
    // saved ones) the first time it shows, and the accounts its drafts are
    // checked against.
    const polled = useRef(false);
    useEffect(() => {
        if (!shown || tab !== 'suggested' || polled.current) return;
        polled.current = true;
        void withSavedSuggestions(fetchSuggested()).then(
            catalog => setSuggested(prev => withCatalog(prev, catalog)),
            e => setSuggested(prev => ({ ...prev, loading: false, requestError: message(e, 'The server did not answer.') })),
        );
        refreshAccounts();
    }, [shown, tab, refreshAccounts]);
    // An account changed in the docked panel: it may make a draft ready.
    const seenRevision = useRef(accountsRevision);
    useEffect(() => {
        if (seenRevision.current === accountsRevision) return;
        seenRevision.current = accountsRevision;
        refreshAccounts();
    }, [accountsRevision, refreshAccounts]);

    const suggestion = suggested.catalog?.suggestions.find(item => item.id === suggested.pickedId);
    const blockers = [
        ...(name.trim() ? [] : ['Name the channel.']),
        ...(tab === 'suggested' ? suggestedBlockers(suggested, suggestion, accounts) : configBlockers(config)),
    ];

    const create = async () => {
        if (blockers.length) return;
        const body: CreateChannelBody = tab === 'suggested' && suggestion && 'saved' in suggestion
            ? { saved: { id: suggestion.id, runners: suggested.drafts.map(item => item.draft) } }
            : tab === 'suggested'
            ? { suggested: { id: suggestion!.id, tutorial: suggested.tutorial && suggestion!.tutorial.length > 0, runners: suggested.drafts.map(item => item.draft) } }
            : { config: configSelection(config) };
        setBusy(true); setError('');
        try { await onCreate(name.trim(), body); }
        catch (e) { setError(message(e, 'Could not create the channel.')); }
        finally { setBusy(false); }
    };

    return <div className="flex flex-col gap-3">
        <div className="flex h-[1.875rem] flex-none items-start border-b border-line" role="tablist" aria-label="Start from">
            {TABS.map(({ id, label }) => <button key={id} type="button" role="tab" id={`create-tab-${id}`} aria-selected={tab === id} aria-controls={`create-panel-${id}`}
                className={`mt-px inline-flex h-[1.125rem] items-center ${tab === id ? tabOn : tabOff}`} disabled={busy}
                onClick={() => setTab(id)}>{label}</button>)}
        </div>
        <div id="create-panel-suggested" role="tabpanel" aria-labelledby="create-tab-suggested" hidden={tab !== 'suggested'} className="pt-1">
            <SuggestedTab state={suggested} update={setSuggested} accounts={accounts} busy={busy}
                onOpenKeys={onOpenKeys} />
        </div>
        <div id="create-panel-config" role="tabpanel" aria-labelledby="create-tab-config" hidden={tab !== 'config'} className="pt-1">
            <ConfigTab config={config} setConfig={setConfig} channels={channels} />
        </div>
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        <div className="mt-4 flex flex-col gap-2 border-t border-line pt-4">
            {blockers.length > 0 && <span className="text-[0.75rem] text-ink-3">{blockers[0]}</span>}
            <div className="flex flex-wrap items-end gap-3">
                {onCancel && <button type="button" className={`${btn} mr-auto`} disabled={busy} onClick={onCancel}>Cancel</button>}
                <div className="flex min-w-0 flex-[1_1_24rem] flex-wrap items-end justify-end gap-2">
                    <label className="flex min-w-[8rem] max-w-md flex-1 flex-col gap-1"><span className={formLabel}>Channel name</span>
                        <input className={formInput} placeholder="channel name" value={name} disabled={busy} onChange={e => setName(e.target.value)} />
                    </label>
                    <button type="button" className={btnPrimary} disabled={busy || blockers.length > 0} onClick={() => void create()}>{busy ? 'Creating…' : 'Create channel'}</button>
                </div>
            </div>
            {savedIn && <p data-saved-in className="text-right text-[0.75rem] text-ink-3">Saved in <span className="font-mono">{savedIn}</span></p>}
        </div>
    </div>;
}
