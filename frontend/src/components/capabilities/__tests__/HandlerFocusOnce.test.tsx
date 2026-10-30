// A handler focus (the Files panel's 'Show in Capabilities') is shown once:
// its card reports it shown, the manage view drops it, and a remount of the
// card (a search hid it, then showed it again) re-runs nothing.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CapabilitiesManageCard, type CapabilitiesManageCardProps } from '../CapabilitiesManageCard';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { acceptHandler, fetchHandlers, inspectHandler, type BuildInspection } from '@/lib/definitionApi';
import { fetchRunnerGenerators, type RunnerEntry } from '@/lib/runnerApi';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchHandlers: vi.fn(), inspectHandler: vi.fn(), acceptHandler: vi.fn(async () => ({ accepted: true })),
}));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), addFromCatalog: vi.fn(),
}));
vi.mock('@/lib/cellSaveBarrier', () => ({ flushCellSaves: vi.fn(async () => {}) }));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[] | null }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: async () => {} }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));

const props = (): CapabilitiesManageCardProps => ({
    stackId: 'caps', width: 480, height: 640, isSelected: true, isFront: true,
    onSelect: vi.fn(), onShowCell: vi.fn(), onHide: vi.fn(),
});
const search = () => screen.getByRole('searchbox', { name: 'Search capabilities' });
const memoCard = () => screen.queryByRole('article', { name: 'Memo handler' });

const BUILT = 'b1b2b3b4b5b6b7b8';
const inspection = (): BuildInspection => ({
    content_hash: 'content-exact-hash', derived_hash: 'd1d2d3d4d5d6d7d8', compile: { valid: true }, examples: [],
});

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner()];
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    // A build waits for review.
    vi.mocked(fetchHandlers).mockResolvedValue([handler({ has_derived: true, built_hash: BUILT }), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
    vi.mocked(inspectHandler).mockResolvedValue(inspection());
});
afterEach(cleanup);

describe('A handler focus is shown once, even when its card remounts', () => {
    it('Show in Capabilities (Review), accept, a search hides the card, the search clears: Review never re-opens', async () => {
        render(<CapabilitiesManageCard {...props()} />);
        await screen.findByRole('article', { name: 'Memo handler' });

        // The Files panel's 'Show in Capabilities' for a waiting build (FilesPanel.showHandler(..., 'review')).
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ kind: 'handler', handler: { cellId: 'views', folder: 'memo', step: 'review' } } as never); });
        const review = await screen.findByRole('region', { name: 'Review build d1d2d3d4' });
        await waitFor(() => expect(inspectHandler).toHaveBeenCalledTimes(1));

        // Accept the build; the catalog now reports it in use.
        vi.mocked(fetchHandlers).mockResolvedValue([handler({ has_derived: true, built_hash: 'd1d2d3d4d5d6d7d8', accepted: true, accepted_derived: 'd1d2d3d4d5d6d7d8' }), builtinHandler()]);
        fireEvent.click(within(review).getByRole('button', { name: 'Accept build d1d2d3d4' }));
        await waitFor(() => expect(acceptHandler).toHaveBeenCalledTimes(1));
        await waitFor(() => expect(screen.queryByRole('region', { name: 'Review build d1d2d3d4' })).toBeNull());
        await waitFor(() => expect(fetchHandlers).toHaveBeenCalledTimes(2));
        expect(await within(memoCard()!).findByRole('status')).toHaveTextContent('Build d1d2d3d4 is in use.');
        // Let any reveal timers settle.
        await act(async () => { await new Promise(r => setTimeout(r, 1500)); });
        expect(inspectHandler).toHaveBeenCalledTimes(1);

        // Type 'foo' in Search capabilities: the card goes away (unmounts).
        fireEvent.change(search(), { target: { value: 'foo' } });
        expect(memoCard()).toBeNull();
        // Clear the search: the card comes back.
        fireEvent.change(search(), { target: { value: '' } });
        expect(memoCard()).not.toBeNull();
        await act(async () => { await new Promise(r => setTimeout(r, 50)); });

        const calls = vi.mocked(inspectHandler).mock.calls.length;
        const reopened = !!screen.queryByRole('region', { name: 'Review build d1d2d3d4' });
        const flashing = memoCard()!.className.includes('border-teal');
        expect(calls).toBe(1);
        expect(reopened).toBe(false);
        expect(flashing).toBe(false);
    });

    it('a search that shows only the other handler, then clears, never re-fires it', async () => {
        render(<CapabilitiesManageCard {...props()} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ kind: 'handler', handler: { cellId: 'views', folder: 'memo', step: 'review' } } as never); });
        const review = await screen.findByRole('region', { name: 'Review build d1d2d3d4' });
        await waitFor(() => expect(inspectHandler).toHaveBeenCalledTimes(1));
        // Close the step instead of accepting.
        fireEvent.click(within(review).getByRole('button', { name: 'Close' }));
        await waitFor(() => expect(screen.queryByRole('region', { name: 'Review build d1d2d3d4' })).toBeNull());
        await act(async () => { await new Promise(r => setTimeout(r, 1500)); });

        fireEvent.change(search(), { target: { value: 'sheet' } });
        expect(memoCard()).toBeNull();
        expect(screen.getByRole('article', { name: 'Sheet handler' })).toBeInTheDocument();
        fireEvent.change(search(), { target: { value: '' } });
        await act(async () => { await new Promise(r => setTimeout(r, 50)); });
        const calls = vi.mocked(inspectHandler).mock.calls.length;
        const reopened = !!screen.queryByRole('region', { name: 'Review build d1d2d3d4' });
        expect(calls).toBe(1);
        expect(reopened).toBe(false);
    });
});
