import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { deleteChannel, type CreateChannelBody } from '@/lib/api/channels';
import { describeApiError } from '@/lib/api/client';
import { archiveChannel, fetchServerInstance, unarchiveChannel, type ManagedChannel, type ServerInstance } from '@/lib/api/storage';
import { desktopBridge } from '@/lib/desktop';
import { useStorage } from '@/hooks/useStorage';
import { useDefaultThemeSurface } from '@/contexts/ThemeContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH } from '@/stores/collimate/commandPaneSlice';
import { btn, iconBtn, tabOff, tabOn } from '@/components/ui/formStyles';
import { ChannelDialog } from '@/components/ChannelDialog';
import { ChannelTable } from './ChannelTable';
import { DeleteChannelDialog } from './DeleteChannelDialog';
import { LocationsTab } from './LocationsTab';
import { MoveChannelDialog } from './MoveChannelDialog';
import { NewTab } from './NewTab';
import { ServersTab } from './ServersTab';
import { SettingsPanel, type SettingsPanelKind } from './SettingsPanel';
import { SettingsTab } from './SettingsTab';
import { StorageTab } from './StorageTab';
import { channelsTabsFor, offeredTab, type ChannelsTab } from './tabs';
import type { ChannelChoice } from './create/state';

export interface ChannelsPageProps {
    /** The tab asked for (StackApp's `channelsPage`); one the page does not
     *  offer (Servers in a browser) shows New. */
    tab: ChannelsTab;
    onTab: (tab: ChannelsTab) => void;
    /** Back to the open channel; absent when there is none (the page is
     *  then the window's only surface). */
    onClose?: () => void;
    /** Rejects on failure, so the New tab shows why and re-enables Create. */
    onCreate: (name: string, body: CreateChannelBody) => Promise<void> | void;
    /** The channels a new one may copy from (none archived). */
    channels: ChannelChoice[];
    /** The open channel: the Config tab's default source. */
    sourceChannelId: string | null;
    /** Select a channel in this window and leave the page; an archived one
     *  opens read-only. Rejects with the server's refusal. */
    onOpenChannel: (id: string) => Promise<void>;
    /** Rename a channel (any of this server's, archived ones too). */
    onRenameChannel: (id: string, name: string) => Promise<void>;
    /** The app's channel list changed here (archive, unarchive, rename):
     *  read the window's state again. */
    onChannelsChanged: () => Promise<void> | void;
    /** Changes whenever the app's own channel list does, so the listing is
     *  read again (a channel created or renamed from elsewhere). */
    revision?: unknown;
}

/** The window's server, as the header names it: the desktop shell's name
 *  for it, else the page's host (a browser against one server). */
function windowServerName(): string {
    return desktopBridge()?.server.name ?? window.location.host;
}

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? '' : 's'}`;

/**
 * New / manage channels (plan 1105 "The page"): one page holding every
 * channel and data control of the window's server, replacing the New
 * channel page. The header names the server (its name, data dir and
 * version); there is no picker, the Servers tab (desktop only) switches the
 * window instead. Tabs: New (Suggested | Config, the name and Create), the
 * Channels table (every channel that is not archived), the Archived table
 * (the only place an archived channel opens from, read-only), both with
 * Move… (MoveChannelDialog, the job's progress on the row) and Delete…
 * (DeleteChannelDialog), Locations (LocationsTab: add, claim, default,
 * remove), Storage (StorageTab: the overview and its cleanups), Servers
 * and Settings (SettingsTab: this machine's accounts, this server's default
 * theme, this profile's preferences). New names the default location
 * ("Saved in …"), or says there is no usable one and links to Locations.
 *
 * The page shows in the server's default theme, also over a channel that
 * overrides it (useDefaultThemeSurface), so the default is on show while it
 * is edited. Accounts and the default theme open in a docked panel
 * (SettingsPanel) on the Commands pane's side; the page's scroller pads by
 * its width there, so the centred column never sits under it, and it stays
 * open across tabs.
 *
 * The New tab stays mounted while another tab shows, so a half-made channel
 * survives a look at the tables. `data-channel-create` marks the page for
 * the desktop shell's New channel handoff and for the Commands pane, which
 * lies over the page rather than under it.
 */
export function ChannelsPage({
    tab: requested, onTab, onClose, onCreate, channels, sourceChannelId,
    onOpenChannel, onRenameChannel, onChannelsChanged, revision,
}: ChannelsPageProps) {
    const desktop = desktopBridge() !== undefined;
    const tabs = channelsTabsFor(desktop);
    const tab = offeredTab(requested, desktop);
    const { manage, locations, refresh, move, startMove, dismissMove } = useStorage(revision);
    const [instance, setInstance] = useState<ServerInstance | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState('');
    const [renaming, setRenaming] = useState<{ row: ManagedChannel; value: string } | null>(null);
    const [moving, setMoving] = useState<ManagedChannel | null>(null);
    const [deleting, setDeleting] = useState<ManagedChannel | null>(null);
    const serverName = windowServerName();
    useDefaultThemeSurface();

    // The docked settings panel. Each account change made in it bumps the
    // revision the New tab reads them again on (the tab stays in view beside
    // the panel); a new draft remounts the KeyManager so it opens on that
    // draft.
    const [panel, setPanel] = useState<SettingsPanelKind | null>(null);
    const [accountsRevision, setAccountsRevision] = useState(0);
    const [draftSeq, setDraftSeq] = useState(0);
    const panelSide = useCollimateStore(s => s.commandPane.side);
    // What last opened the panel from outside it: it gets the keyboard back
    // on close while it is still on show; else (its tab unmounted it or is
    // hidden) the current tab's button does, never the body.
    const opener = useRef<HTMLElement | null>(null);
    const openPanel = (next: SettingsPanelKind) => {
        const active = document.activeElement;
        if (active instanceof HTMLElement && !active.closest('[data-settings-panel]')) opener.current = active;
        if (next.kind === 'keys' && next.draft) setDraftSeq(n => n + 1);
        setPanel(next);
    };
    const closePanel = () => {
        setPanel(null);
        const back = opener.current;
        opener.current = null;
        if (back?.isConnected && !back.closest('[hidden]')) back.focus();
        else document.getElementById(`channels-tab-${tab}`)?.focus();
    };
    const accountsChanged = useCallback(() => setAccountsRevision(n => n + 1), []);

    useEffect(() => {
        let live = true;
        fetchServerInstance().then(next => { if (live) setInstance(next); }, () => {});
        return () => { live = false; };
    }, []);

    const rows = manage.channels ?? [];
    const listed = rows.filter(row => !row.archived);
    const archived = rows.filter(row => row.archived);

    /** One row action at a time; its refusal shows above the table. A
     *  change reads the listing again, then `after` (the app's state). */
    const act = async (row: ManagedChannel, op: () => Promise<unknown>, after?: () => Promise<unknown> | void) => {
        setBusy(row.id); setError('');
        try {
            await op();
            await refresh();
            await after?.();
        } catch (e) {
            setError(describeApiError(e));
        } finally {
            setBusy(null);
        }
    };
    // Opening leaves the page (StackApp closes it once the channel is
    // selected), so nothing is read again here.
    const open = async (row: ManagedChannel) => {
        if (row.active && onClose) { onClose(); return; }
        setBusy(row.id); setError('');
        try { await onOpenChannel(row.id); }
        catch (e) { setError(describeApiError(e)); }
        finally { setBusy(null); }
    };
    const archive = (row: ManagedChannel) => void act(row, () => archiveChannel(row.id), onChannelsChanged);
    const unarchive = (row: ManagedChannel) => void act(row, () => unarchiveChannel(row.id), onChannelsChanged);
    const submitRename = () => {
        if (!renaming) return;
        const { row, value } = renaming;
        setRenaming(null);
        const name = value.trim();
        if (name && name !== row.name) void act(row, () => onRenameChannel(row.id, name));
    };

    // Delete: the dialog shows a refusal and stays open; done, the listing
    // and the app's state are read again (the active channel may be gone).
    const confirmDelete = async () => {
        if (!deleting) return;
        await deleteChannel(deleting.id);
        setDeleting(null);
        await refresh();
        await onChannelsChanged();
    };
    // A claim adopts channels into the window's list.
    const locationsChanged = async () => {
        await refresh();
        await onChannelsChanged();
    };
    const closeMove = () => { setMoving(null); dismissMove(); };

    const locationList = locations.list;
    const locationPath = (id: string) => locationList?.find(location => location.id === id)?.path;
    const defaultLocation = locationList?.find(location => location.is_default) ?? null;
    const usableDefault = defaultLocation && defaultLocation.state === 'ok' ? defaultLocation : null;
    const tableMove = move ? { channelId: move.channelId, job: move.job } : null;

    const listingNote = manage.error
        ? <p role="alert" className="text-[0.75rem] text-red">Could not read this server&rsquo;s channels: {manage.error}</p>
        : manage.channels === null ? <p className="text-[0.78125rem] text-ink-3">Reading this server&rsquo;s channels…</p> : null;

    let body: ReactNode = null;
    if (tab === 'channels') {
        body = <>{listingNote}{manage.channels && <ChannelTable label="Channels" rows={listed} serverName={serverName} busy={busy}
            empty="No channels on this server yet. Make one on the New tab." locationPath={locationPath} moving={tableMove}
            onOpen={row => void open(row)} onRename={row => setRenaming({ row, value: row.name })} onArchive={archive}
            onMove={setMoving} onDelete={setDeleting} />}</>;
    } else if (tab === 'archived') {
        body = <>
            <p className="text-[0.78125rem] text-ink-3">An archived channel is left out of every channel list and opens read-only: nothing in it can change or run until it is unarchived.</p>
            {listingNote}
            {manage.channels && <ChannelTable label="Archived channels" rows={archived} serverName={serverName} busy={busy}
                empty="No archived channels." openLabel="Open read-only" locationPath={locationPath} moving={tableMove}
                onOpen={row => void open(row)} onRename={row => setRenaming({ row, value: row.name })} onUnarchive={unarchive}
                onMove={setMoving} onDelete={setDeleting} />}
        </>;
    } else if (tab === 'locations') {
        body = <LocationsTab locations={locations} channels={manage.channels} onChanged={locationsChanged} />;
    } else if (tab === 'storage') {
        body = <StorageTab shown />;
    } else if (tab === 'servers') {
        body = <ServersTab />;
    } else if (tab === 'settings') {
        body = <SettingsTab onOpen={kind => openPanel({ kind })} />;
    }

    return <div role="dialog" aria-modal="true" aria-label="Channels" data-channel-create data-channels-tab={tab}
        className="absolute inset-0 z-[400] flex bg-canvas text-ink">
        <div data-channels-scroll className="flex min-w-0 flex-1 flex-col overflow-y-auto [scrollbar-gutter:stable]"
            // A margin, not padding: the scroll box itself narrows, so its
            // scrollbar stays beside the panel rather than under it.
            style={panel ? { [panelSide === 'left' ? 'marginLeft' : 'marginRight']: COMMAND_PANE_WIDTH } : undefined}>
            <div className="mx-auto my-12 flex w-full max-w-3xl flex-col gap-3 px-5">
                <header className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                        <h1 className="truncate text-xl font-semibold text-ink">{serverName}</h1>
                        <p data-server-identity className="mt-0.5 truncate text-[0.75rem] text-ink-3">
                            {instance
                                ? <><span className="font-mono" title="This server's data dir">{instance.data_dir}</span> · Collimate v{instance.version}</>
                                : 'This window’s server'}
                        </p>
                    </div>
                    {onClose && <button type="button" aria-label="Close channels page" title="Back to the channel" onClick={onClose}
                        className={cn(iconBtn, 'h-7 w-7')}><X className="size-4" /></button>}
                </header>
                <div className="mt-2 flex h-[1.875rem] flex-none items-start border-b border-line" role="tablist" aria-label="Channels page">
                    {tabs.map(({ id, label }) => <button key={id} type="button" role="tab" id={`channels-tab-${id}`} aria-selected={tab === id}
                        aria-controls={`channels-panel-${id}`}
                        className={`mt-px inline-flex h-[1.125rem] items-center ${tab === id ? tabOn : tabOff}`}
                        onClick={() => onTab(id)}>
                        {label}
                        {id === 'archived' && archived.length > 0 && <span className="text-ink-3">&nbsp;{archived.length}</span>}
                    </button>)}
                </div>
                {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
                {/* Kept mounted: switching tabs keeps a half-made channel. */}
                <div id="channels-panel-new" role="tabpanel" aria-labelledby="channels-tab-new" hidden={tab !== 'new'} className="flex flex-col gap-3 pt-1">
                    {!onClose && archived.length > 0 && <p data-archived-hint className="flex flex-wrap items-center gap-2 text-[0.78125rem] text-ink-2">
                        This server has {plural(archived.length, 'archived channel')}.
                        <button type="button" className={btn} onClick={() => onTab('archived')}>Show archived</button>
                    </p>}
                    {locationList && !usableDefault && <p role="alert" data-no-default-location className="flex flex-wrap items-center gap-2 text-[0.78125rem] text-red">
                        {defaultLocation
                            ? <>The default location <span className="font-mono">{defaultLocation.path}</span> is {defaultLocation.state === 'foreign' ? 'owned by another installation' : defaultLocation.state}: new channels cannot be made until another is the default.</>
                            : <>This server has no default location for new channels.</>}
                        <button type="button" className={btn} onClick={() => onTab('locations')}>Locations</button>
                    </p>}
                    <NewTab shown={tab === 'new'} onCreate={onCreate} onCancel={onClose} channels={channels} sourceChannelId={sourceChannelId}
                        savedIn={usableDefault?.path ?? null} onOpenKeys={draft => openPanel({ kind: 'keys', draft })}
                        accountsRevision={accountsRevision} />
                </div>
                {tab !== 'new' && <div id={`channels-panel-${tab}`} role="tabpanel" aria-labelledby={`channels-tab-${tab}`} className="flex flex-col gap-3 pt-1">
                    {body}
                </div>}
            </div>
        </div>
        {panel && <SettingsPanel key={`${panel.kind}:${draftSeq}`} panel={panel} onClose={closePanel} onAccountsChanged={accountsChanged} />}
        {renaming && <ChannelDialog channelName={renaming.row.name} value={renaming.value}
            onChange={value => setRenaming(prev => prev && { ...prev, value })}
            onCancel={() => setRenaming(null)} onSubmit={submitRename} />}
        {moving && <MoveChannelDialog channel={moving} locations={locationList ?? []} move={move}
            onStart={locationId => startMove(moving, locationId)} onClose={closeMove} />}
        {deleting && <DeleteChannelDialog channel={deleting} onCancel={() => setDeleting(null)} onConfirm={confirmDelete} />}
    </div>;
}
