import { useState, type ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import type { CleanupSelection } from '@/lib/api/storage';
import { formatBytes } from '@/lib/fileInfo';
import { btn, btnDanger, btnPrimary, formInput } from '@/components/ui/formStyles';
import { PageDialog } from './PageDialog';

/** The plan's default for "not used in the last N days". */
export const DEFAULT_UNUSED_DAYS = 30;

/** What a preview answers, in the shape both cleanups share. */
export interface CleanupPreview<Item> {
    preview_id: string;
    count: number;
    bytes: number;
    /** False: `bytes` is a lower bound ("at least"). */
    exact: boolean;
    items: Item[];
}

/** What a delete answers, in the shape both cleanups share. */
export interface CleanupResult {
    removed: string[];
    failed: { name: string; reason: string }[];
    bytes: number;
    exact: boolean;
}

export interface CleanupDialogProps<Item> {
    /** "Docker images", "Run environments". */
    title: string;
    /** What the things are, plural ("images", "environments"). */
    noun: string;
    /** A sentence above the choice (what is shared, what rebuilds). */
    intro: ReactNode;
    /** Shown instead of the choice when the cleanup cannot run (Docker not
     *  reachable); the dialog then only closes. */
    unavailable?: string | null;
    preview: (selection: CleanupSelection) => Promise<CleanupPreview<Item>>;
    remove: (previewId: string) => Promise<CleanupResult>;
    /** One preview row: its name and the line under it. */
    describe: (item: Item) => { key: string; name: string; detail: string };
    onClose: () => void;
    /** After a delete: the page reads the overview again. */
    onDone: () => void;
}

/**
 * A cleanup in three steps (plan "The page", Storage): All | Not used in
 * the last [30] days → the preview (count, size, exact or "at least", the
 * list) → Delete → what was removed, what failed and why, and the size
 * freed. A preview is used once and lasts ten minutes; the server checks
 * each item again before it goes. Docker images… and Run environments…
 * are this dialog over their routes.
 */
export function CleanupDialog<Item>({ title, noun, intro, unavailable, preview, remove, describe, onClose, onDone }: CleanupDialogProps<Item>) {
    const [mode, setMode] = useState<'all' | 'unused'>('unused');
    const [days, setDays] = useState(String(DEFAULT_UNUSED_DAYS));
    const [shown, setShown] = useState<CleanupPreview<Item> | null>(null);
    const [result, setResult] = useState<CleanupResult | null>(null);
    const [busy, setBusy] = useState<'preview' | 'delete' | null>(null);
    const [error, setError] = useState('');

    const dayCount = Number.parseInt(days, 10);
    const validDays = Number.isFinite(dayCount) && dayCount >= 0 && String(dayCount) === days.trim();
    const selection: CleanupSelection = mode === 'all' ? { mode } : { mode, days: dayCount };

    const runPreview = async () => {
        if (mode === 'unused' && !validDays) return;
        setBusy('preview'); setError(''); setShown(null);
        try { setShown(await preview(selection)); }
        catch (e) { setError(describeApiError(e)); }
        finally { setBusy(null); }
    };
    const runDelete = async () => {
        if (!shown) return;
        setBusy('delete'); setError('');
        try {
            setResult(await remove(shown.preview_id));
            setShown(null);
            onDone();
        } catch (e) {
            // A used or expired preview: preview again.
            setError(describeApiError(e));
            setShown(null);
        } finally {
            setBusy(null);
        }
    };

    const amount = (bytes: number, exact: boolean) => `${exact ? '' : 'at least '}${formatBytes(bytes)}`;
    const footer = result || unavailable
        ? <button type="button" className={btn} onClick={onClose}>Close</button>
        : <>
            <button type="button" className={btn} disabled={busy !== null} onClick={onClose}>Cancel</button>
            {shown
                ? <button type="button" className={btnDanger} disabled={busy !== null || shown.count === 0} onClick={() => void runDelete()}>
                    {busy === 'delete' ? 'Deleting…' : `Delete ${shown.count} ${noun}`}
                </button>
                : <button type="button" className={btnPrimary} disabled={busy !== null || (mode === 'unused' && !validDays)} onClick={() => void runPreview()}>
                    {busy === 'preview' ? 'Previewing…' : 'Preview'}
                </button>}
        </>;

    return <PageDialog label={title} title={title} onClose={onClose} busy={busy !== null} footer={footer} wide>
        <div>{intro}</div>
        {unavailable && <p role="alert" data-cleanup-unavailable className="text-red">{unavailable}</p>}
        {!unavailable && !result && <fieldset className="space-y-1.5" aria-label="Which to delete" disabled={busy !== null}>
            <label className="flex items-center gap-2">
                <input type="radio" name="cleanup-mode" checked={mode === 'all'} onChange={() => { setMode('all'); setShown(null); }} />
                <span>All {noun}</span>
            </label>
            <label className="flex flex-wrap items-center gap-2">
                <input type="radio" name="cleanup-mode" checked={mode === 'unused'} onChange={() => { setMode('unused'); setShown(null); }} />
                <span>Not used in the last</span>
                <input type="number" min={0} aria-label="Days" value={days} className={cn(formInput, 'w-20')}
                    onChange={e => { setDays(e.target.value); setMode('unused'); setShown(null); }} />
                <span>days</span>
            </label>
        </fieldset>}
        {shown && <div data-cleanup-preview className="space-y-2">
            <p className="text-ink">
                {shown.count === 0
                    ? `Nothing to delete: no ${noun} match.`
                    : `${shown.count} ${noun}, ${amount(shown.bytes, shown.exact)}.`}
            </p>
            {shown.items.length > 0 && <ul className="max-h-60 space-y-1 overflow-y-auto rounded-md border border-line p-2">
                {shown.items.map(item => {
                    const row = describe(item);
                    return <li key={row.key} className="min-w-0">
                        <span className="block break-all font-mono text-[0.75rem] text-ink">{row.name}</span>
                        <span className="block text-[0.71875rem] text-ink-3">{row.detail}</span>
                    </li>;
                })}
            </ul>}
        </div>}
        {result && <div role="status" data-cleanup-result className="space-y-1.5">
            <p className="text-ink">Deleted {result.removed.length} {noun}, freeing {result.exact ? 'about' : 'at least'} {formatBytes(result.bytes)}.</p>
            {result.failed.length > 0 && <>
                <p>Kept {result.failed.length}:</p>
                <ul className="space-y-0.5">
                    {result.failed.map(failed => <li key={failed.name}><span className="font-mono">{failed.name}</span>: {failed.reason}</li>)}
                </ul>
            </>}
        </div>}
        {error && <p role="alert" className="text-red">{error}</p>}
    </PageDialog>;
}
