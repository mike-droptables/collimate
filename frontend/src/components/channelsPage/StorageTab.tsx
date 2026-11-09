import { useState, type ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import {
    clearScratch, collectNow, deleteDockerImages, deleteRunEnvironments, previewDockerImages, previewRunEnvironments, pruneUvCache,
    type DockerImage, type RunEnvironment, type StorageOverview,
} from '@/lib/api/storage';
import { formatBytes } from '@/lib/fileInfo';
import { useStorageOverview } from '@/hooks/useStorage';
import { btn, btnPrimary, card, sectionLabel } from '@/components/ui/formStyles';
import { CleanupDialog, type CleanupPreview, type CleanupResult } from './CleanupDialog';
import { PageDialog } from './PageDialog';

/** A time the server gave, in the viewer's words. */
export function when(iso: string | null | undefined): string {
    if (!iso) return 'never';
    const at = new Date(iso);
    return Number.isNaN(at.getTime()) ? iso : at.toLocaleString();
}

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? '' : 's'}`;

/** The overview's part names, as `survey.pending` lists them. */
const PART_LABEL: Record<string, string> = {
    app_db: 'app.db', app_store: 'app bodies', staged_store: 'staged bodies', envs: 'run environments', scratch: 'scratch',
    previous_layout: 'the previous layout', other: 'other', uv_cache: 'uv’s cache', adapters: 'agent adapters', docker: 'Docker images',
};

/** One stacked-bar segment: the server's own parts, which make its total. */
interface Segment { key: string; label: string; bytes: number; tone: string }

/** The server's own parts, largest first: each location, then the data
 *  dir's parts (what `totals.bytes` adds up). */
export function segments(overview: StorageOverview): Segment[] {
    const out: Segment[] = [];
    overview.locations.forEach((location, index) => {
        if (location.state !== 'foreign' && location.bytes) {
            out.push({ key: `location:${location.id}`, label: location.path, bytes: location.bytes, tone: index % 2 ? 'bg-teal/60' : 'bg-teal' });
        }
    });
    const add = (key: string, label: string, bytes: number | undefined, tone: string) => { if (bytes) out.push({ key, label, bytes, tone }); };
    add('app_db', 'app.db', overview.app_db?.bytes, 'bg-brass');
    add('app_store', 'App bodies', (overview.app_store?.bytes ?? 0) + (overview.app_store?.trash_bytes ?? 0), 'bg-brass/60');
    add('staged_store', 'Staged bodies', (overview.staged_store?.bytes ?? 0) + (overview.staged_store?.trash_bytes ?? 0), 'bg-brass/35');
    add('envs', 'Run environments', overview.envs?.bytes, 'bg-green');
    add('scratch', 'Scratch', (overview.scratch?.tmp_bytes ?? 0) + (overview.scratch?.pycache_bytes ?? 0), 'bg-ink-3');
    add('previous_layout', 'Previous layout', overview.previous_layout?.bytes, 'bg-red/50');
    add('other', 'Other', overview.other?.bytes, 'bg-line-2');
    return out;
}

function UsageBar({ overview }: { overview: StorageOverview }) {
    const parts = segments(overview);
    const total = parts.reduce((sum, part) => sum + part.bytes, 0);
    if (total <= 0) return null;
    return <div className="space-y-1.5">
        <div role="img" aria-label={`Usage: ${parts.map(part => `${part.label} ${formatBytes(part.bytes)}`).join(', ')}`}
            className="flex h-2.5 w-full overflow-hidden rounded-full bg-sheet-3">
            {parts.map(part => <div key={part.key} title={`${part.label}: ${formatBytes(part.bytes)}`} className={cn('h-full', part.tone)}
                style={{ width: `${(part.bytes / total) * 100}%` }} />)}
        </div>
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[0.71875rem] text-ink-3">
            {parts.map(part => <li key={part.key} className="flex min-w-0 items-center gap-1">
                <span className={cn('inline-block h-2 w-2 flex-none rounded-full', part.tone)} />
                <span className="max-w-[14rem] truncate">{part.label}</span>
            </li>)}
        </ul>
    </div>;
}

/** One row of a section: what, its size (or why there is none), a line
 *  under it, and its actions. */
function Row({ name, label, size, detail, children }: { name: string; label: ReactNode; size: ReactNode; detail?: ReactNode; children?: ReactNode }) {
    return <li data-storage-row={name} className="flex flex-wrap items-start gap-x-3 gap-y-1 border-b border-line px-3 py-2 last:border-b-0">
        <div className="min-w-0 flex-1">
            <div className="text-ink">{label}</div>
            {detail && <div className="text-[0.71875rem] text-ink-3">{detail}</div>}
        </div>
        <div className="whitespace-nowrap text-right text-ink-2">{size}</div>
        {children && <div className="flex w-full flex-wrap justify-end gap-1.5 sm:w-auto">{children}</div>}
    </li>;
}

type Dialog = 'docker' | 'envs' | 'uv' | null;

/**
 * The Storage tab (plan 1105 "The page", P8): one stacked usage bar of the
 * server's own parts, then each part with its size and an "as of" time,
 * from `GET /api/storage` (the survey's cache; Refresh measures again, and
 * a running survey shows what it is still measuring). The server's rows:
 * channels by location, app.db, app and staged bodies, run environments
 * (Run environments…), scratch (Clear scratch), other, the previous layout.
 * The machine's, reported apart and never in the total: Docker images
 * (Docker images…), uv's cache (Prune uv's cache…, behind its warning) and
 * the agent adapters. Collection: when it last ran, and Run collection now.
 */
export function StorageTab({ shown }: { shown: boolean }) {
    const { overview, error, refresh, reload } = useStorageOverview(shown);
    const [dialog, setDialog] = useState<Dialog>(null);
    const [busy, setBusy] = useState<'scratch' | 'collect' | null>(null);
    const [note, setNote] = useState<{ key: string; text: string; failed?: boolean } | null>(null);

    const run = async (key: 'scratch' | 'collect', op: () => Promise<string>) => {
        setBusy(key); setNote(null);
        try { setNote({ key, text: await op() }); }
        catch (e) { setNote({ key, text: describeApiError(e), failed: true }); }
        finally { setBusy(null); void reload(); }
    };
    const scratch = () => run('scratch', async () => {
        const cleared = await clearScratch();
        const kept = cleared.kept.length ? ` Kept ${cleared.kept.length}: ${cleared.kept.map(item => `${item.name} (${item.reason})`).join('; ')}.` : '';
        return `Cleared scratch: freed ${formatBytes(cleared.bytes_freed)}.${kept}`;
    });
    const collect = () => run('collect', async () => {
        const result = await collectNow();
        return `Collected: freed ${formatBytes(result.freed_bytes)}.`;
    });

    if (!overview) {
        return error
            ? <p role="alert" className="text-[0.75rem] text-red">Could not read this server&rsquo;s storage: {error}</p>
            : <p className="text-[0.78125rem] text-ink-3">Reading this server&rsquo;s storage…</p>;
    }

    const survey = overview.survey;
    const pending = new Set(survey.state === 'running' ? survey.pending ?? [] : []);
    /** A part's size, or why there is none yet. */
    const sized = (part: string, bytes: number | undefined, exact = true) => bytes !== undefined
        ? <>{exact ? '' : 'at least '}{formatBytes(bytes)}{pending.has(part) && <span className="block text-[0.6875rem] text-ink-3">measuring…</span>}</>
        : <span className="text-ink-3">{pending.has(part) ? 'measuring…' : '—'}</span>;
    const asOf = (iso: string | null | undefined) => iso ? `as of ${when(iso)}` : null;
    const docker = overview.docker;
    const dockerOff = docker && !docker.available ? docker.reason : null;
    const noteFor = (key: string) => note?.key === key
        ? <p role={note.failed ? 'alert' : 'status'} className={cn('w-full text-right text-[0.71875rem]', note.failed ? 'text-red' : 'text-ink-2')}>{note.text}</p>
        : null;

    return <>
        <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-0 flex-1">
                <p className="text-[0.84375rem] text-ink">
                    <span data-storage-total className="font-semibold">{formatBytes(overview.totals.bytes)}</span> on this server
                </p>
                <p data-survey-state={survey.state} className="text-[0.71875rem] text-ink-3">
                    {survey.state === 'running'
                        ? <>Measuring…{pending.size > 0 && <> still: {[...pending].map(part => PART_LABEL[part] ?? part).join(', ')}</>}</>
                        : survey.state === 'failed'
                            ? <span className="text-red">The last survey failed{survey.error ? `: ${survey.error}` : ''}.</span>
                            : survey.computed_at ? `Measured ${when(survey.computed_at)}` : 'Not measured yet'}
                </p>
            </div>
            <button type="button" className={btn} disabled={survey.state === 'running'} onClick={() => void refresh()}>
                {survey.state === 'running' ? 'Measuring…' : 'Refresh'}
            </button>
        </div>
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        <UsageBar overview={overview} />

        <h2 className={sectionLabel}>This server</h2>
        <ul aria-label="This server's storage" className={cn(card, 'text-[0.78125rem]')}>
            {overview.locations.map(location => <Row key={location.id} name={`location:${location.id}`}
                label={<span className="flex flex-wrap items-center gap-1.5"><span className="break-all font-mono text-[0.75rem]">{location.path}</span>
                    {location.is_default && <span className="text-[0.6875rem] text-teal">Default</span>}</span>}
                size={location.state === 'foreign' ? <span className="text-ink-3">—</span> : formatBytes(location.bytes ?? 0)}
                detail={location.state === 'foreign'
                    ? <>Owned by another installation (<span className="font-mono">{location.owner_data_dir ?? 'unknown'}</span>){location.channel_count != null ? ` · ${plural(location.channel_count, 'channel')}` : ''}</>
                    : <>
                        {plural(location.channel_count ?? 0, 'channel')}
                        {location.free_bytes != null && location.total_bytes != null && <> · {formatBytes(location.free_bytes)} free of {formatBytes(location.total_bytes)}</>}
                        {location.state !== 'ok' && <> · <span className="text-red">{location.state}</span></>}
                        {(location.channels?.length ?? 0) > 0 && <details className="mt-1">
                            <summary className="cursor-pointer">Its channels</summary>
                            <ul className="mt-1 space-y-0.5">
                                {location.channels!.map(channel => <li key={channel.id} className="flex gap-2">
                                    <span className="min-w-0 flex-1 truncate text-ink-2">{channel.name}{channel.archived ? ' (archived)' : ''}</span>
                                    <span className="whitespace-nowrap" title={channel.computed_at ? `As of ${when(channel.computed_at)}` : 'Not sized yet'}>
                                        {channel.bytes != null ? formatBytes(channel.bytes) : '—'}
                                    </span>
                                </li>)}
                            </ul>
                        </details>}
                    </>} />)}
            <Row name="app_db" label="app.db" size={sized('app_db', overview.app_db?.bytes)}
                detail={<>The server&rsquo;s own database (installation, locations, catalogs){asOf(overview.app_db?.computed_at) && <> · {asOf(overview.app_db?.computed_at)}</>}</>} />
            <Row name="app_store" label="App bodies" size={sized('app_store', overview.app_store && overview.app_store.bytes + overview.app_store.trash_bytes)}
                detail={<>Renderer bundles, mirrored modules, handler builds{overview.app_store ? ` · ${formatBytes(overview.app_store.trash_bytes)} in trash` : ''}{asOf(overview.app_store?.computed_at) && <> · {asOf(overview.app_store?.computed_at)}</>}</>} />
            <Row name="staged_store" label="Staged bodies" size={sized('staged_store', overview.staged_store && overview.staged_store.bytes + overview.staged_store.trash_bytes)}
                detail={<>Uploads waiting for a new channel{asOf(overview.staged_store?.computed_at) && <> · {asOf(overview.staged_store?.computed_at)}</>}</>} />
            <Row name="envs" label="Run environments" size={sized('envs', overview.envs?.bytes)}
                detail={overview.envs
                    ? <>{overview.envs.count} environments · {formatBytes(overview.envs.reclaimable_bytes)} reclaimable{asOf(overview.envs.computed_at) && <> · {asOf(overview.envs.computed_at)}</>}</>
                    : 'Each generator version’s tree and uv environment'}>
                <button type="button" className={btn} onClick={() => setDialog('envs')}>Run environments…</button>
            </Row>
            <Row name="scratch" label="Scratch" size={sized('scratch', overview.scratch && overview.scratch.tmp_bytes + overview.scratch.pycache_bytes)}
                detail={overview.scratch
                    ? <>Run workspaces and leftovers {formatBytes(overview.scratch.tmp_bytes)} ({formatBytes(overview.scratch.tmp_live_bytes)} in use by {overview.scratch.live_runs} live runs) · bytecode {formatBytes(overview.scratch.pycache_bytes)}{asOf(overview.scratch.computed_at) && <> · {asOf(overview.scratch.computed_at)}</>}</>
                    : 'Run workspaces, leftovers and bytecode'}>
                <button type="button" className={btn} disabled={busy !== null} onClick={() => void scratch()}>{busy === 'scratch' ? 'Clearing…' : 'Clear scratch'}</button>
                {noteFor('scratch')}
            </Row>
            <Row name="other" label="Other" size={sized('other', overview.other?.bytes)}
                detail={<>Imports, verifier workers, keys and the like{asOf(overview.other?.computed_at) && <> · {asOf(overview.other?.computed_at)}</>}</>} />
            {overview.previous_layout && <Row name="previous_layout" label="Previous layout" size={sized('previous_layout', overview.previous_layout.bytes)}
                detail={<>{overview.previous_layout.note} <span className="font-mono break-all">{overview.previous_layout.paths.join(', ')}</span></>} />}
            <Row name="collection" label="Body collection" size={<span className="text-ink-3">last ran {when(overview.last_collection_at)}</span>}
                detail="Removes bodies nothing refers to any more; it runs by itself once a day">
                <button type="button" className={btn} disabled={busy !== null} onClick={() => void collect()}>{busy === 'collect' ? 'Collecting…' : 'Run collection now'}</button>
                {noteFor('collect')}
            </Row>
        </ul>

        <h2 className={sectionLabel}>Shared by this machine</h2>
        <p className="text-[0.75rem] text-ink-3">Every Collimate server on this machine uses these; they are not in the total above. Another server may rebuild what is deleted here.</p>
        <ul aria-label="Shared storage" className={cn(card, 'text-[0.78125rem]')}>
            <Row name="docker" label="Docker images"
                size={docker?.available ? sized('docker', docker.images_bytes, docker.exact) : sized('docker', undefined)}
                detail={!docker
                    ? 'The Collimate-built images on the Docker daemon'
                    : docker.available
                        ? <>{docker.count} images · {docker.unused_count} not used here in {docker.unused_days} days ({docker.unused_exact ? '' : 'at least '}{formatBytes(docker.unused_bytes)}) · build cache {formatBytes(docker.build_cache_bytes)} (reported only){asOf(docker.computed_at) && <> · {asOf(docker.computed_at)}</>}</>
                        : <span data-docker-unavailable>Docker is not available: {docker.reason}</span>}>
                <button type="button" className={btn} onClick={() => setDialog('docker')}>Docker images…</button>
            </Row>
            <Row name="uv_cache" label="uv’s cache" size={sized('uv_cache', overview.uv_cache?.bytes)}
                detail={overview.uv_cache
                    ? <><span className="font-mono break-all">{overview.uv_cache.path ?? overview.uv_cache.reason ?? 'unknown'}</span>{asOf(overview.uv_cache.computed_at) && <> · {asOf(overview.uv_cache.computed_at)}</>}</>
                    : 'Python packages and environments for every tool that uses uv'}>
                <button type="button" className={btn} onClick={() => setDialog('uv')}>Prune uv&rsquo;s cache…</button>
            </Row>
            <Row name="adapters" label="Agent adapters" size={sized('adapters', overview.adapters?.bytes)}
                detail={overview.adapters
                    ? <><span className="font-mono break-all">{overview.adapters.path}</span> · reported only{asOf(overview.adapters.computed_at) && <> · {asOf(overview.adapters.computed_at)}</>}</>
                    : 'The agent adapters’ install cache (reported only)'} />
        </ul>

        {dialog === 'docker' && <CleanupDialog<DockerImage> title="Docker images" noun="images"
            intro={<p>Collimate-built images on this machine&rsquo;s Docker daemon. &ldquo;Not used&rdquo; counts this server&rsquo;s recorded uses only, so another server&rsquo;s images count as unused here; it rebuilds them on its next run. An image with a container or another tag is never deleted.</p>}
            unavailable={dockerOff ? `Docker is not available: ${dockerOff}` : null}
            preview={async selection => {
                const answer = await previewDockerImages(selection);
                return { ...answer, items: answer.images } satisfies CleanupPreview<DockerImage>;
            }}
            remove={async id => {
                const answer = await deleteDockerImages(id);
                return { removed: answer.removed, failed: answer.failed.map(f => ({ name: f.ref, reason: f.reason })), bytes: answer.bytes_freed_estimate, exact: answer.exact } satisfies CleanupResult;
            }}
            describe={image => ({ key: image.id, name: image.refs.join(', ') || image.id,
                detail: `${formatBytes(image.unique_size)} its own · last used here ${image.last_used_at ? when(image.last_used_at) : 'never recorded'}` })}
            onClose={() => setDialog(null)} onDone={() => void reload()} />}
        {dialog === 'envs' && <CleanupDialog<RunEnvironment> title="Run environments" noun="environments"
            intro={<p>Each generator version&rsquo;s read-only tree and its uv environment. A deleted one is rebuilt on the next run that needs it; one a run uses now is never selected.</p>}
            preview={async selection => {
                const answer = await previewRunEnvironments(selection);
                return { ...answer, items: answer.envs } satisfies CleanupPreview<RunEnvironment>;
            }}
            remove={async id => {
                const answer = await deleteRunEnvironments(id);
                return { removed: answer.removed, failed: answer.failed.map(f => ({ name: f.key, reason: f.reason })), bytes: answer.bytes_freed_estimate, exact: true } satisfies CleanupResult;
            }}
            describe={env => ({ key: env.key, name: `${env.generator_id} · ${env.key}`,
                detail: `${formatBytes(env.reclaimable_bytes)} · last used ${when(env.last_used_at)}` })}
            onClose={() => setDialog(null)} onDone={() => void reload()} />}
        {dialog === 'uv' && <UvPruneDialog onClose={() => setDialog(null)} onDone={() => void reload()} />}
    </>;
}

/** Prune uv's cache…: behind its warning, since it affects every tool on
 *  the machine that uses uv; refused while a run is live here. */
function UvPruneDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<{ ok: boolean; output: string } | null>(null);
    const [error, setError] = useState('');
    const prune = async () => {
        setBusy(true); setError('');
        try { setResult(await pruneUvCache()); onDone(); }
        catch (e) { setError(describeApiError(e)); }
        finally { setBusy(false); }
    };
    return <PageDialog label="Prune uv's cache" title="Prune uv’s Cache" onClose={onClose} busy={busy}
        footer={result
            ? <button type="button" className={btn} onClick={onClose}>Close</button>
            : <>
                <button type="button" className={btn} disabled={busy} onClick={onClose}>Cancel</button>
                <button type="button" className={btnPrimary} disabled={busy} onClick={() => void prune()}>{busy ? 'Pruning…' : 'Prune'}</button>
            </>}>
        <p data-uv-warning className="text-brass">This affects the whole machine: <span className="font-mono">uv cache prune</span> removes what uv no longer needs for every tool that uses uv, not only Collimate. Environments are rebuilt when next needed.</p>
        {result && <div role="status" className="space-y-1">
            <p className={result.ok ? 'text-ink' : 'text-red'}>{result.ok ? 'uv pruned its cache.' : 'uv could not prune its cache.'}</p>
            {result.output && <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-line p-2 font-mono text-[0.71875rem]">{result.output}</pre>}
        </div>}
        {error && <p role="alert" className="text-red">{error}</p>}
    </PageDialog>;
}
