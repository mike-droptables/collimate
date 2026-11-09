import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StorageTab, segments } from './StorageTab';
import {
    clearScratch, collectNow, deleteDockerImages, deleteRunEnvironments, fetchStorageOverview, previewDockerImages, previewRunEnvironments,
    pruneUvCache, refreshStorage, type StorageOverview,
} from '@/lib/api/storage';
import { retiredOr } from '@/test/retiredTokens';

// The Storage tab (plan 1105 P8) against mocked routes: the overview's
// rows and total, what a running survey still measures, Refresh, Docker
// images… and Run environments… (choice → preview with count, size, exact
// or "at least" → delete → the result), Docker unavailable, Clear scratch
// and its 409, Run collection now, and Prune uv's cache… behind its warning.

vi.mock('@/lib/api/storage', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/api/storage')>(),
    fetchStorageOverview: vi.fn(), refreshStorage: vi.fn(), clearScratch: vi.fn(), collectNow: vi.fn(),
    previewDockerImages: vi.fn(), deleteDockerImages: vi.fn(), previewRunEnvironments: vi.fn(), deleteRunEnvironments: vi.fn(), pruneUvCache: vi.fn(),
}));

const MB = 2 ** 20;
const AT = '2026-11-05T10:00:00Z';
const overview = (over: Partial<StorageOverview> = {}): StorageOverview => ({
    server: { data_dir: '/srv/c', instance: 'i', installation_id: 'inst', version: '0.9.0' },
    survey: { state: 'idle', computed_at: AT },
    totals: { bytes: 60 * MB, computed_at: AT },
    locations: [
        { id: 'home', path: '/home/me/Collimate/Channels', is_default: true, state: 'ok', channel_count: 1, bytes: 40 * MB, other_bytes: 4096,
            free_bytes: 10 * 2 ** 30, total_bytes: 100 * 2 ** 30,
            channels: [{ id: 'a', name: 'Alpha', archived: true, bytes: 40 * MB, files: 10, parts: null, computed_at: AT }] },
        { id: 'old', path: '/srv/old', is_default: false, state: 'foreign', owner_data_dir: '/srv/other', channel_count: 2 },
    ],
    app_db: { bytes: 2 * MB, computed_at: AT },
    app_store: { bytes: 5 * MB, trash_bytes: MB, computed_at: AT },
    staged_store: { bytes: 0, trash_bytes: 0, computed_at: AT },
    envs: { count: 3, bytes: 8 * MB, reclaimable_bytes: 6 * MB, computed_at: AT },
    scratch: { tmp_bytes: 3 * MB, tmp_live_bytes: MB, pycache_bytes: MB, live_runs: 1, computed_at: AT },
    other: { bytes: MB, computed_at: AT },
    uv_cache: { path: '/home/me/.cache/uv', bytes: 900 * MB, computed_at: AT },
    adapters: { path: '/home/me/.cache/collimate/acp-adapters', bytes: 20 * MB, computed_at: AT },
    docker: { available: true, count: 4, images_bytes: 3000 * MB, exact: false, unused_days: 30, unused_count: 2, unused_bytes: 1000 * MB,
        unused_exact: false, build_cache_bytes: 50 * MB, computed_at: AT },
    last_collection_at: AT,
    ...over,
});
const rowOf = (name: string) => document.querySelector(`[data-storage-row="${name}"]`) as HTMLElement;

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchStorageOverview).mockResolvedValue(overview());
});
afterEach(() => cleanup());

describe('the Storage tab (plan 1105 P8)', () => {
    it('shows the total, the bar and each row: the server’s parts, then the machine’s apart', async () => {
        render(<StorageTab shown />);
        expect(await screen.findByText('60 MB')).toHaveAttribute('data-storage-total');
        expect(screen.getByRole('img', { name: /Usage:/ })).toBeInTheDocument();
        expect(rowOf('location:home')).toHaveTextContent('40 MB');
        expect(rowOf('location:home')).toHaveTextContent('1 channel · 10 GB free of 100 GB');
        expect(rowOf('location:home')).toHaveTextContent('Alpha (archived)');
        expect(rowOf('location:old')).toHaveTextContent('Owned by another installation (/srv/other) · 2 channels');
        expect(rowOf('app_db')).toHaveTextContent('2.0 MB');
        expect(rowOf('app_store')).toHaveTextContent('6.0 MB');
        expect(rowOf('envs')).toHaveTextContent('3 environments · 6.0 MB reclaimable');
        expect(rowOf('scratch')).toHaveTextContent('4.0 MB');
        expect(rowOf('docker')).toHaveTextContent('at least 2.9 GB');
        expect(rowOf('docker')).toHaveTextContent('2 not used here in 30 days (at least 1000 MB)');
        expect(rowOf('uv_cache')).toHaveTextContent('/home/me/.cache/uv');
        expect(rowOf('adapters')).toHaveTextContent('reported only');
        expect(screen.getByRole('list', { name: 'Shared storage' })).toContainElement(rowOf('docker'));
        expect(segments(overview()).map(s => s.key)).toEqual(['location:home', 'app_db', 'app_store', 'envs', 'scratch', 'other']);
    });

    it('a running survey says what it still measures and reads again until it ends; Refresh starts one', async () => {
        vi.mocked(fetchStorageOverview)
            .mockResolvedValueOnce(overview({ survey: { state: 'running', computed_at: AT, started_at: AT, pending: ['uv_cache', 'docker'] }, uv_cache: undefined }))
            .mockResolvedValue(overview());
        vi.mocked(refreshStorage).mockResolvedValue({ survey: { state: 'running', computed_at: AT, pending: [] } });
        render(<StorageTab shown />);
        const state = await waitFor(() => { const el = document.querySelector('[data-survey-state="running"]'); expect(el).not.toBeNull(); return el as HTMLElement; });
        expect(state).toHaveTextContent('Measuring… still: uv’s cache, Docker images');
        expect(rowOf('uv_cache')).toHaveTextContent('measuring…');
        expect(screen.getByRole('button', { name: 'Measuring…' })).toBeDisabled();
        await waitFor(() => expect(document.querySelector('[data-survey-state="idle"]')).not.toBeNull(), { timeout: 3000 });
        fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
        await waitFor(() => expect(refreshStorage).toHaveBeenCalledOnce());
        await waitFor(() => expect(fetchStorageOverview).toHaveBeenCalledTimes(3));
    });

    it('Docker images…: choose, preview the count and size (at least), delete, see what went and what stayed', async () => {
        vi.mocked(previewDockerImages).mockResolvedValue({ preview_id: 'p1', count: 2, bytes: 1000 * MB, exact: false, expires_at: AT, images: [
            { id: 'sha:1', refs: ['collimate-env:abc'], other_refs: [], size: 1, shared_size: 0, unique_size: 600 * MB, containers: 0, created_at: AT },
            { id: 'sha:2', refs: ['collimate-run:def'], other_refs: [], size: 1, shared_size: 0, unique_size: 400 * MB, containers: 0, created_at: AT, last_used_at: AT },
        ] });
        vi.mocked(deleteDockerImages).mockResolvedValue({ removed: ['collimate-env:abc'], failed: [{ ref: 'collimate-run:def', reason: 'a run is preparing it' }], bytes_freed_estimate: 600 * MB, exact: false });
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        fireEvent.click(screen.getByRole('button', { name: 'Docker images…' }));
        const dialog = await screen.findByRole('dialog', { name: 'Docker images' });
        expect(dialog).toHaveTextContent('another server’s images count as unused here');
        fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Days' }), { target: { value: '14' } });
        fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));
        await waitFor(() => expect(previewDockerImages).toHaveBeenCalledExactlyOnceWith({ mode: 'unused', days: 14 }));
        const preview = await waitFor(() => { const el = dialog.querySelector('[data-cleanup-preview]'); expect(el).not.toBeNull(); return el as HTMLElement; });
        expect(preview).toHaveTextContent('2 images, at least 1000 MB.');
        expect(preview).toHaveTextContent('collimate-env:abc');
        expect(preview).toHaveTextContent('last used here never recorded');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 images' }));
        await waitFor(() => expect(deleteDockerImages).toHaveBeenCalledExactlyOnceWith('p1'));
        const result = await within(dialog).findByRole('status');
        expect(result).toHaveTextContent('Deleted 1 images, freeing at least 600 MB.');
        expect(result).toHaveTextContent('collimate-run:def: a run is preparing it');
        // The overview is read again after the delete.
        await waitFor(() => expect(fetchStorageOverview).toHaveBeenCalledTimes(2));
    });

    it('without Docker the row says why and the dialog only explains', async () => {
        vi.mocked(fetchStorageOverview).mockResolvedValue(overview({ docker: { available: false, reason: 'Cannot connect to the Docker daemon', computed_at: AT } }));
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        expect(rowOf('docker')).toHaveTextContent('Docker is not available: Cannot connect to the Docker daemon');
        fireEvent.click(screen.getByRole('button', { name: 'Docker images…' }));
        const dialog = await screen.findByRole('dialog', { name: 'Docker images' });
        expect(dialog.querySelector('[data-cleanup-unavailable]')).toHaveTextContent('Cannot connect to the Docker daemon');
        expect(within(dialog).queryByRole('button', { name: 'Preview' })).toBeNull();
    });

    it('Run environments…: all, preview, delete; an expired preview says so', async () => {
        vi.mocked(previewRunEnvironments).mockResolvedValue({ preview_id: 'e1', count: 1, bytes: 6 * MB, exact: true, envs: [
            { key: 'k1', generator_id: 'plot', tree: '/t', bytes: 7 * MB, reclaimable_bytes: 6 * MB, last_used_at: AT, live: false },
        ] });
        vi.mocked(deleteRunEnvironments).mockRejectedValueOnce(Object.assign(new Error('x'), { response: { status: 404, data: { detail: 'This preview has expired or was used; preview again' } } }))
            .mockResolvedValue({ removed: ['k1'], failed: [], bytes_freed_estimate: 6 * MB });
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        fireEvent.click(screen.getByRole('button', { name: 'Run environments…' }));
        const dialog = await screen.findByRole('dialog', { name: 'Run environments' });
        fireEvent.click(within(dialog).getByRole('radio', { name: 'All environments' }));
        fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));
        await waitFor(() => expect(previewRunEnvironments).toHaveBeenCalledExactlyOnceWith({ mode: 'all' }));
        expect(await within(dialog).findByText('1 environments, 6.0 MB.')).toBeInTheDocument();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete 1 environments' }));
        expect(await within(dialog).findByRole('alert')).toHaveTextContent('preview again');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));
        fireEvent.click(await within(dialog).findByRole('button', { name: 'Delete 1 environments' }));
        expect(await within(dialog).findByRole('status')).toHaveTextContent('Deleted 1 environments, freeing about 6.0 MB.');
    });

    it('Clear scratch says what it freed, or the server’s refusal; Run collection now says what it freed', async () => {
        vi.mocked(clearScratch)
            .mockRejectedValueOnce(Object.assign(new Error('x'), { response: { status: 409, data: { detail: 'A run is starting, live or finishing on this server.' } } }))
            .mockResolvedValue({ bytes_freed: 3 * MB, removed: 2, kept: [{ name: 'ws_1', reason: 'its run’s receipt does not prove it abandoned' }] });
        vi.mocked(collectNow).mockResolvedValue({ channels: [], app_store: {}, staging: {}, git: [], freed_bytes: 512 * 1024, last_collection_at: AT });
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        fireEvent.click(screen.getByRole('button', { name: 'Clear scratch' }));
        expect(await within(rowOf('scratch')).findByRole('alert')).toHaveTextContent('A run is starting, live or finishing');
        fireEvent.click(screen.getByRole('button', { name: 'Clear scratch' }));
        expect(await within(rowOf('scratch')).findByRole('status')).toHaveTextContent('Cleared scratch: freed 3.0 MB. Kept 1: ws_1');
        fireEvent.click(screen.getByRole('button', { name: 'Run collection now' }));
        expect(await within(rowOf('collection')).findByRole('status')).toHaveTextContent('Collected: freed 512 KB.');
    });

    it('Prune uv’s cache… warns that it affects the whole machine, then shows uv’s output or the refusal', async () => {
        vi.mocked(pruneUvCache)
            .mockRejectedValueOnce(Object.assign(new Error('x'), { response: { status: 409, data: { detail: 'Runs are still running or finishing on this server.' } } }))
            .mockResolvedValue({ ok: true, output: 'Removed 12 files' });
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        fireEvent.click(screen.getByRole('button', { name: 'Prune uv’s cache…' }));
        const dialog = await screen.findByRole('dialog', { name: "Prune uv's cache" });
        expect(dialog.querySelector('[data-uv-warning]')).toHaveTextContent('This affects the whole machine');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Prune' }));
        expect(await within(dialog).findByRole('alert')).toHaveTextContent('Runs are still running');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Prune' }));
        expect(await within(dialog).findByRole('status')).toHaveTextContent('Removed 12 files');
    });

    it('a failed read says so; nothing is read while hidden', async () => {
        render(<StorageTab shown={false} />);
        expect(fetchStorageOverview).not.toHaveBeenCalled();
        cleanup();
        vi.mocked(fetchStorageOverview).mockRejectedValue(new Error('offline'));
        render(<StorageTab shown />);
        expect(await screen.findByRole('alert')).toHaveTextContent('Could not read this server’s storage: offline');
    });

    it('paints from the skin tokens (S04)', async () => {
        render(<StorageTab shown />);
        await screen.findByText('60 MB');
        fireEvent.click(screen.getByRole('button', { name: 'Docker images…' }));
        await screen.findByRole('dialog', { name: 'Docker images' });
        const painted = [...document.body.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});
