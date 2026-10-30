import { useCallback, useEffect, useRef, useState } from 'react';
import type { CreateChannelBody } from '@/lib/api/channels';
import { fetchSuggested, setSuggestedSource, type SuggestedCatalog } from '@/lib/api/suggested';
import { fetchAccounts, type Account } from '@/lib/accountsApi';
import { btn, btnPrimary, formInput, formLabel, tabOff, tabOn } from '@/components/ui/formStyles';
import { KeyManagerOverlay } from './settings/KeyManager';
import { ConfigTab } from './channelCreate/ConfigTab';
import { SuggestedTab } from './channelCreate/SuggestedTab';
import { configBlockers, configSelection, suggestedBlockers, suggestedDrafts } from './channelCreate/createBody';
import { initialConfig, initialSuggested, initialTab, type ChannelChoice, type CreateTab, type SuggestedState } from './channelCreate/state';

const TABS: { id: CreateTab; label: string }[] = [
    { id: 'suggested', label: 'Suggested' },
    { id: 'config', label: 'Config' },
];

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** A catalog answered: the picked suggestion stays (with its drafts) when
 *  it is still offered and the source is the same; else the first one,
 *  with fresh drafts. A folder source keeps its row open. */
function withCatalog(prev: SuggestedState, catalog: SuggestedCatalog, sourceChanged: boolean): SuggestedState {
    const kept = sourceChanged ? undefined : catalog.suggestions.find(item => item.id === prev.pickedId);
    const picked = kept ?? catalog.suggestions[0];
    const folder = catalog.source.kind === 'folder';
    return {
        ...prev, loading: false, catalog, requestError: '',
        pickedId: picked?.id ?? '',
        drafts: kept ? prev.drafts : picked ? suggestedDrafts(picked) : [],
        confirming: kept ? prev.confirming : null,
        folderOpen: prev.folderOpen || folder,
        folderPath: folder ? catalog.source.path : prev.folderPath,
        folderBusy: false, folderError: '',
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
}

/**
 * The new-channel page: two tabs, with the channel name beside Create in
 * the footer. Suggested starts from the SDK's suggested catalog; Config
 * assembles the channel from existing channels and files. It opens on Config
 * when channels exist to copy from,
 * else on Suggested. Every tab's state lives here, and both tab bodies stay
 * mounted (the inactive one hidden), so switching tabs keeps the work and
 * the fetches under way.
 */
export function ChannelCreateView({ onCreate, onCancel, channels, sourceChannelId }: Props) {
    const [name, setName] = useState('');
    const [tab, setTab] = useState<CreateTab>(() => initialTab(channels));
    const [suggested, setSuggested] = useState(initialSuggested);
    const [config, setConfig] = useState(() => initialConfig(channels, sourceChannelId));
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [keysOpen, setKeysOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const refreshAccounts = useCallback(() => {
        void fetchAccounts().then(data => setAccounts(data.accounts), e => setError(message(e, 'Could not read the accounts.')));
    }, []);
    // Catalog requests are numbered: only the latest one's answer lands, so
    // a slow Refresh never overwrites a newer change of source.
    const latestCatalog = useRef(0);
    const readCatalog = useCallback((read: () => Promise<SuggestedCatalog>, sourceChanged: boolean) => {
        const request = ++latestCatalog.current;
        const latest = () => request === latestCatalog.current;
        return read().then(
            catalog => { if (latest()) setSuggested(prev => withCatalog(prev, catalog, sourceChanged)); },
            e => {
                if (!latest()) return;
                setSuggested(prev => sourceChanged
                    ? { ...prev, loading: false, folderBusy: false, folderError: message(e, 'That folder cannot be used.') }
                    : { ...prev, loading: false, folderBusy: false, requestError: message(e, 'The server did not answer.') });
            },
        );
    }, []);

    // The Suggested tab reads the catalog from its source the first time it
    // shows (only Refresh fetches from GitHub), and reads the accounts its
    // drafts are checked against.
    const polled = useRef(false);
    useEffect(() => {
        if (tab !== 'suggested' || polled.current) return;
        polled.current = true;
        void readCatalog(() => fetchSuggested(), false);
        refreshAccounts();
    }, [tab, readCatalog, refreshAccounts]);

    const refresh = () => {
        setSuggested(prev => ({ ...prev, loading: true }));
        void readCatalog(() => fetchSuggested(true), false);
    };
    const chooseSource = (path: string | null) => {
        setSuggested(prev => ({ ...prev, folderBusy: true, folderError: '' }));
        void readCatalog(() => setSuggestedSource(path), true);
    };

    const suggestion = suggested.catalog?.suggestions.find(item => item.id === suggested.pickedId);
    const blockers = [
        ...(name.trim() ? [] : ['Name the channel.']),
        ...(tab === 'suggested' ? suggestedBlockers(suggested, suggestion, accounts) : configBlockers(config)),
    ];

    const create = async () => {
        if (blockers.length) return;
        const body: CreateChannelBody = tab === 'suggested'
            ? { suggested: { id: suggestion!.id, tutorial: suggested.tutorial && suggestion!.tutorial.length > 0, runners: suggested.drafts.map(item => item.draft) } }
            : { config: configSelection(config) };
        setBusy(true); setError('');
        try { await onCreate(name.trim(), body); }
        catch (e) { setError(message(e, 'Could not create the channel.')); }
        finally { setBusy(false); }
    };

    // data-channel-create: the Commands pane lies over this view rather than
    // under it, so it does not count the view as a modal over the pane.
    return <div role="dialog" aria-modal="true" aria-label="Create channel" data-channel-create className="absolute inset-0 z-[400] flex flex-col overflow-y-auto bg-canvas text-ink">
        <div className="mx-auto my-16 flex w-full max-w-2xl flex-col gap-3 px-5">
            <h1 className="text-xl font-semibold text-ink">New channel</h1>
            <div className="mt-2 flex h-[1.875rem] flex-none items-start border-b border-line" role="tablist" aria-label="Start from">
                {TABS.map(({ id, label }) => <button key={id} type="button" role="tab" id={`create-tab-${id}`} aria-selected={tab === id} aria-controls={`create-panel-${id}`}
                    className={`mt-px inline-flex h-[1.125rem] items-center ${tab === id ? tabOn : tabOff}`} disabled={busy}
                    onClick={() => setTab(id)}>{label}</button>)}
            </div>
            <div id="create-panel-suggested" role="tabpanel" aria-labelledby="create-tab-suggested" hidden={tab !== 'suggested'} className="pt-1">
                <SuggestedTab state={suggested} update={setSuggested} accounts={accounts} busy={busy}
                    onRefresh={refresh} onSource={chooseSource} onOpenKeys={() => setKeysOpen(true)} />
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
            </div>
        </div>
        {keysOpen && <KeyManagerOverlay onClose={() => { setKeysOpen(false); refreshAccounts(); }} />}
    </div>;
}
