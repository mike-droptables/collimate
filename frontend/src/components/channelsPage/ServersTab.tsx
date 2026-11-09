import { useEffect, useState } from 'react';
import { Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import { btn, btnDanger, btnPrimary, card, chip, formInput, formLabel } from '@/components/ui/formStyles';
import {
    desktopBridge,
    type ServerEntry,
    type ServerProbeResult,
    type ServerStatus,
    type ServersOps,
    type ShellPrefs,
} from '@/lib/desktop';
import { useServerCatalog } from '@/hooks/useServerCatalog';
import { withFlushedEditors } from '@/lib/rendererFlush';
import { Switch } from '@/components/settings/ToggleRow';

// ─── Servers tab ─────────────────────────────────────────────────────────────
// The channels page's Servers tab, desktop only (moved from Settings →
// Servers, plan 1105): the shell's local server and the saved remotes, one
// card each. It is the page's server switcher: the page is always about the
// window's server (its card says "This window"), and Open reopens the window
// on another one, whose page then shows that server's channels. The shell
// owns the registry, the credentials and the polling; this tab only
// shows entries and asks for changes. A token is typed here and
// handed to the shell once, never read back (`hasToken` is all the page
// learns). Removing a server, changing a token and stopping the local one
// are confirmed by the shell's own dialog. Open recreates the window on that
// server's origin; Stop moves a local window to an available remote before
// shutting local down. Both save the renderer editors first. Keeping the
// local server running at quit is offered in the local server's own card,
// and only while it runs: the shell keeps a server at quit only when it is
// running. Each card names the server's version beside this app's; a server
// whose bridge or API contract this app cannot speak is marked Incompatible
// with the shell's sentence (which side is older, and where to update it)
// and is not offered Open. Mixed versions with one contract are fine, and a
// server is updated on its own machine, never from here. Each remote card
// has a "Check and connect" switch: off keeps the saved record (name, URL,
// token, certificate pin) but the shell stops polling it, never opens it at
// launch or hands a window to it, and refuses to open it until it is turned
// on again; the card then shows Off with no version, channels or Open.
// Turning it on polls it at once. The window's own server cannot be turned
// off, as it cannot be removed; the local server has no switch. The app's
// own version and build stamp live on the Settings tab, under This profile.
// Until the shell's first answer the list reads "Loading servers…": a list
// waits for the shell to poll every server, and an unresponsive remote holds
// that poll until its timeout, so the tab must not open empty.

const STATUS_LABEL: Record<ServerStatus, string> = {
    starting: 'Starting',
    running: 'Running',
    stopped: 'Stopped',
    unreachable: 'Unreachable',
    unauthorized: 'Token refused',
    incompatible: 'Incompatible',
    failed: 'Failed',
    off: 'Off',
};

const STATUS_TONE: Record<ServerStatus, string> = {
    starting: 'text-ink-2',
    running: 'text-green',
    stopped: 'text-ink-3',
    unreachable: 'text-red',
    unauthorized: 'text-red',
    incompatible: 'text-red',
    failed: 'text-red',
    off: 'text-ink-3',
};

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

export function ServersTab() {
    const shell = desktopBridge();
    if (!shell) return null;
    return <ServersPanel servers={shell.servers} hasLocal={shell.platform !== 'win32'} appVersion={shell.buildInfo().version} />;
}

function ServersPanel({ servers, hasLocal, appVersion }: {
    servers: ServersOps;
    hasLocal: boolean;
    appVersion: string;
}) {
    // Bumped after every change so the catalog lists again at once, besides
    // the shell's own push.
    const [revision, setRevision] = useState(0);
    const entries = useServerCatalog(revision);
    const hasAvailableRemote = entries?.some(entry => entry.kind === 'remote' && entry.status === 'running') ?? false;
    const [prefs, setPrefs] = useState<ShellPrefs | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [adding, setAdding] = useState(false);

    useEffect(() => {
        let live = true;
        servers.getPrefs().then(p => { if (live) setPrefs(p); }, () => {});
        return () => { live = false; };
    }, [servers]);

    /** One shell request at a time; its refusal shows above the list. */
    const run = async (key: string, op: () => Promise<unknown>): Promise<boolean> => {
        setBusy(key);
        setError(null);
        try {
            await op();
            return true;
        } catch (e) {
            setError(message(e, 'The desktop shell refused that.'));
            return false;
        } finally {
            setBusy(null);
            setRevision(r => r + 1);
        }
    };

    const setKeepLocal = (keepLocalRunning: boolean) => void run('prefs', async () => {
        setPrefs(await servers.setPrefs({ keepLocalRunning }));
    });

    return (
        <div className="space-y-4">
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}

            {entries === null ? (
                <p role="status" data-servers-loading className="text-[0.75rem] text-ink-3">Loading servers…</p>
            ) : (
                <ul className="space-y-2" aria-label="Servers">
                    {entries.map(entry => (
                        <ServerRow
                            key={entry.id}
                            entry={entry}
                            busy={busy}
                            run={run}
                            servers={servers}
                            hasLocal={hasLocal}
                            hasAvailableRemote={hasAvailableRemote}
                            prefs={prefs}
                            onKeepLocal={setKeepLocal}
                            appVersion={appVersion}
                        />
                    ))}
                </ul>
            )}

            {adding ? (
                <AddServerForm
                    servers={servers}
                    onDone={() => { setAdding(false); setRevision(r => r + 1); }}
                    onCancel={() => setAdding(false)}
                />
            ) : (
                <button type="button" className={btn} onClick={() => setAdding(true)}>
                    <Plus className="w-3.5 h-3.5" />
                    Add server
                </button>
            )}
        </div>
    );
}

function ServerRow({ entry, busy, run, servers, hasLocal, hasAvailableRemote, prefs, onKeepLocal, appVersion }: {
    entry: ServerEntry;
    busy: string | null;
    run: (key: string, op: () => Promise<unknown>) => Promise<boolean>;
    servers: ServersOps;
    hasLocal: boolean;
    hasAvailableRemote: boolean;
    prefs: ShellPrefs | null;
    onKeepLocal: (keep: boolean) => void;
    appVersion: string;
}) {
    const [editing, setEditing] = useState<null | 'rename' | 'token'>(null);
    const [value, setValue] = useState('');
    const local = entry.kind === 'local';
    const key = (verb: string) => `${verb}:${entry.id}`;
    const disabled = busy !== null;
    const channelCount = entry.channels
        ? `${entry.channels.length} channel${entry.channels.length === 1 ? '' : 's'}`
        : null;

    const startEdit = (mode: 'rename' | 'token') => {
        setValue(mode === 'rename' ? entry.name : '');
        setEditing(mode);
    };
    const closeEdit = () => { setEditing(null); setValue(''); };
    const submitEdit = async () => {
        const next = value.trim();
        if (!next) return;
        const patch = editing === 'rename' ? { id: entry.id, name: next } : { id: entry.id, token: next };
        if (await run(key(editing ?? 'edit'), () => servers.update(patch))) closeEdit();
    };

    return (
        <li className={cn(card, 'px-3 py-2.5')} data-server-id={entry.id}>
            <div className="flex items-center gap-2 min-w-0">
                <span className="min-w-0 truncate text-[0.8125rem] font-medium text-ink">{entry.name}</span>
                <span className={cn('ml-auto flex-none text-[0.75rem]', STATUS_TONE[entry.status])}>
                    {STATUS_LABEL[entry.status]}
                </span>
            </div>
            <div className="flex flex-wrap items-center gap-1.5 mt-1">
                <span className={chip}>{local ? 'Local' : 'Remote'}</span>
                {entry.isWindowServer && <span className={cn(chip, 'text-brass')}>This window</span>}
            </div>
            <div className="mt-1 truncate font-mono text-[0.75rem] text-ink-3">{entry.url}</div>
            <div className="flex flex-wrap items-center gap-x-2 text-[0.75rem] text-ink-3">
                {entry.enabled && (
                    <span data-versions>
                        Server {entry.serverVersion ? `v${entry.serverVersion}` : 'version unknown'} · this app v{appVersion}
                    </span>
                )}
                {channelCount && <span>{channelCount}</span>}
                {!local && <span>{entry.hasToken ? 'Token saved' : 'No token'}</span>}
            </div>
            {entry.incompatible && (
                <p data-incompatible className="mt-1 text-[0.71875rem] text-red">
                    <span className="font-medium">This app cannot speak this server's API.</span> {entry.incompatible}
                </p>
            )}
            {entry.error && <p className="mt-1 text-[0.71875rem] text-red">{entry.error}</p>}

            {editing ? (
                <form
                    className="flex flex-wrap items-center gap-2 mt-2"
                    onSubmit={e => { e.preventDefault(); void submitEdit(); }}
                >
                    <input
                        autoFocus
                        type={editing === 'token' ? 'password' : 'text'}
                        aria-label={editing === 'rename' ? `Name for ${entry.name}` : `New token for ${entry.name}`}
                        placeholder={editing === 'token' ? 'New token' : undefined}
                        autoComplete="off"
                        value={value}
                        onChange={e => setValue(e.target.value)}
                        className={formInput}
                    />
                    <button type="submit" className={btnPrimary} disabled={disabled || !value.trim()}>Save</button>
                    <button type="button" className={btn} onClick={closeEdit}>Cancel</button>
                </form>
            ) : (
                <div className="flex flex-wrap items-center gap-2 mt-2">
                    {!entry.isWindowServer && entry.status === 'running' && (
                        <button
                            type="button"
                            className={btnPrimary}
                            disabled={disabled}
                            title="Reopen this window on this server"
                            onClick={() => void run(key('open'), () => withFlushedEditors(() => servers.openServer(entry.id)))}
                        >
                            Open
                        </button>
                    )}
                    {local && hasLocal && (entry.status === 'running' || entry.status === 'starting') && (
                        <button
                            type="button"
                            className={btn}
                            disabled={disabled || (entry.isWindowServer && !hasAvailableRemote)}
                            title={entry.isWindowServer && !hasAvailableRemote
                                ? 'Add a running remote server before stopping the server this window uses'
                                : undefined}
                            onClick={() => void run(key('stop'), () => (entry.isWindowServer
                                ? withFlushedEditors(() => servers.stopLocal())
                                : servers.stopLocal()))}
                        >
                            Stop
                        </button>
                    )}
                    {local && hasLocal && entry.status === 'running' && (
                        <label
                            className="flex basis-full items-center gap-2 text-[0.75rem] text-ink-2 cursor-pointer"
                            title="The next launch reuses it when its version matches, and restarts it otherwise."
                        >
                            <Switch
                                checked={prefs?.keepLocalRunning ?? false}
                                label="Keep local server running when the app quits"
                                disabled={!prefs || busy === 'prefs'}
                                onChange={onKeepLocal}
                            />
                            Keep local server running when the app quits
                        </label>
                    )}
                    {local && hasLocal && (entry.status === 'stopped' || entry.status === 'failed') && (
                        <button
                            type="button"
                            className={btn}
                            disabled={disabled}
                            onClick={() => void run(key('start'), () => servers.startLocal())}
                        >
                            Start
                        </button>
                    )}
                    {!local && (
                        <>
                            <button type="button" className={btn} disabled={disabled} onClick={() => startEdit('rename')}>
                                Rename
                            </button>
                            <button type="button" className={btn} disabled={disabled} onClick={() => startEdit('token')}>
                                Change token
                            </button>
                            <button
                                type="button"
                                className={btnDanger}
                                disabled={disabled || entry.isWindowServer}
                                title={entry.isWindowServer ? 'Open another server before removing this one' : undefined}
                                onClick={() => void run(key('remove'), () => servers.remove(entry.id))}
                            >
                                Remove
                            </button>
                            <label
                                className="flex basis-full items-center gap-2 text-[0.75rem] text-ink-2 cursor-pointer"
                                title={entry.isWindowServer && entry.enabled
                                    ? 'Open another server before turning this one off'
                                    : 'Off keeps this server saved, but Collimate never checks or opens it until it is on again.'}
                            >
                                <Switch
                                    checked={entry.enabled}
                                    label="Check and connect"
                                    disabled={disabled || (entry.isWindowServer && entry.enabled)}
                                    onChange={enabled => void run(key('enabled'), () => servers.update({ id: entry.id, enabled }))}
                                />
                                Check and connect
                            </label>
                        </>
                    )}
                </div>
            )}
        </li>
    );
}

function AddServerForm({ servers, onDone, onCancel }: {
    servers: ServersOps;
    onDone: () => void;
    onCancel: () => void;
}) {
    const [name, setName] = useState('');
    const [url, setUrl] = useState('');
    const [token, setToken] = useState('');
    const [busy, setBusy] = useState(false);
    const [probe, setProbe] = useState<ServerProbeResult | null>(null);
    const [error, setError] = useState<string | null>(null);

    const target = () => ({ url: url.trim(), token: token || undefined });

    const test = async () => {
        setBusy(true); setProbe(null); setError(null);
        try {
            setProbe(await servers.probe(target()));
        } catch (e) {
            setError(message(e, 'Could not reach that server.'));
        } finally { setBusy(false); }
    };

    const add = async () => {
        setBusy(true); setProbe(null); setError(null);
        try {
            await servers.add({ ...target(), name: name.trim() || undefined });
            onDone();
        } catch (e) {
            setError(message(e, 'Could not add that server.'));
            setBusy(false);
        }
    };

    const failure = error ?? (probe && !probe.ok ? probe.message : null);
    return (
        <form
            aria-label="Add server"
            className={cn(card, 'p-3 space-y-2.5')}
            onSubmit={e => { e.preventDefault(); void add(); }}
        >
            <label className="flex flex-col gap-1">
                <span className={formLabel}>Name (optional)</span>
                <input value={name} onChange={e => setName(e.target.value)} placeholder="Lab server" className={formInput} />
            </label>
            <label className="flex flex-col gap-1">
                <span className={formLabel}>URL</span>
                <input
                    value={url}
                    onChange={e => { setUrl(e.target.value); setProbe(null); }}
                    placeholder="https://collimate.example.com"
                    autoComplete="off"
                    className={formInput}
                />
            </label>
            <label className="flex flex-col gap-1">
                <span className={formLabel}>Token (optional)</span>
                <input
                    type="password"
                    value={token}
                    onChange={e => { setToken(e.target.value); setProbe(null); }}
                    autoComplete="off"
                    className={formInput}
                />
            </label>
            {failure && <p role="alert" className="text-[0.75rem] text-red">{failure}</p>}
            {probe?.ok && <p role="status" className="text-[0.75rem] text-green">Reachable · Collimate {probe.serverVersion}</p>}
            <div className="flex flex-wrap items-center gap-2">
                <button type="submit" className={btnPrimary} disabled={busy || !url.trim()}>Add</button>
                <button type="button" className={btn} disabled={busy || !url.trim()} onClick={() => void test()}>Test</button>
                <button type="button" className={btn} onClick={onCancel}>Cancel</button>
            </div>
        </form>
    );
}
