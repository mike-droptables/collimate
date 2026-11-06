import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { ChannelReadOnlyContext } from '@/contexts/ChannelReadOnlyContext';
import { CapabilitiesManageCard, type CapabilitiesManageCardProps } from '../CapabilitiesManageCard';
import { CapabilitiesManageBody } from '../CapabilitiesManageBody';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityCatalog, fetchCapabilityLibraries, fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchHandlers } from '@/lib/definitionApi';
import { fetchRunnerGenerators, type RunnerEntry } from '@/lib/runnerApi';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

// An archived channel only reads the Capabilities manage view (plan 1105 P7).
vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/userCatalog', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCatalogEntries: vi.fn(async () => []), fetchSavedSuggestions: vi.fn(async () => []),
}));
vi.mock('@/lib/api/capabilityCells', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityUpdates: vi.fn(async () => []), reviewReplace: vi.fn(), replaceFromUpstream: vi.fn(),
}));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), fetchCapabilityLibraries: vi.fn(), addFromCatalog: vi.fn(), fetchCapabilityCatalog: vi.fn(),
}));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[], refresh: vi.fn(async () => {}) }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: runnerMocks.refresh }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));
vi.mock('@/components/runner/RunnerEditor', () => ({ RunnerEditor: () => <div data-testid="runner-editor" /> }));

const props = (): CapabilitiesManageCardProps => ({
    stackId: 'caps', width: 480, height: 640, isSelected: true, isFront: true,
    onSelect: vi.fn(), onShowCell: vi.fn(), onHide: vi.fn(),
});
const withReadOnly = (readOnly: boolean | undefined, node: React.ReactNode) =>
    readOnly === undefined ? node : <ChannelReadOnlyContext.Provider value={readOnly}>{node}</ChannelReadOnlyContext.Provider>;
async function mountCard(readOnly?: boolean) {
    const view = render(withReadOnly(readOnly, <CapabilitiesManageCard {...props()} />));
    await screen.findByRole('article', { name: 'Memo handler' });
    return view;
}
const search = () => screen.getByRole('searchbox', { name: 'Search capabilities' });
const queryButton = (name: string) => screen.queryByRole('button', { name });

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner()];
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler(), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
    vi.mocked(fetchCapabilityLibraries).mockResolvedValue([]);
    vi.mocked(fetchCapabilityCatalog).mockResolvedValue([] as never);
});
afterEach(cleanup);

describe('The Capabilities manage view in an archived channel', () => {
    it('without the context the toolbar offers Create… and Add from catalog… and no fieldset wraps the groups (plan 1105 P7)', async () => {
        const { container } = await mountCard();
        expect(queryButton('Create…')).not.toBeNull();
        expect(queryButton('Add from catalog…')).not.toBeNull();
        expect(container.querySelector('fieldset[data-capabilities-read-only]')).toBeNull();
    });

    it('with the context true there is no Create… or Add from catalog…, the groups sit in a disabled fieldset and search stays usable (plan 1105 P7)', async () => {
        const { container } = await mountCard(true);
        expect(queryButton('Create…')).toBeNull();
        expect(queryButton('Add from catalog…')).toBeNull();
        const fieldset = container.querySelector('fieldset[data-capabilities-read-only]') as HTMLFieldSetElement;
        expect(fieldset).not.toBeNull();
        expect(fieldset.disabled).toBe(true);
        expect(within(fieldset).getByRole('article', { name: 'Memo handler' })).toBeInTheDocument();
        expect((search() as HTMLInputElement).disabled).toBe(false);
        expect(search().closest('fieldset')).toBeNull();
    });

    it('with the context true a request for Create or the catalog lands on the list (plan 1105 P7)', async () => {
        await mountCard(true);
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ create: true } as never); });
        await waitFor(() => expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull());
        expect(screen.queryByRole('group', { name: 'What should this channel learn to do?' })).toBeNull();
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ addFromCatalog: true } as never); });
        await waitFor(() => expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull());
        expect(screen.getByRole('article', { name: 'Memo handler' })).toBeInTheDocument();
        expect(queryButton('Add from catalog…')).toBeNull();
    });

    it('without the context the same Create request opens the questionnaire (plan 1105 P7)', async () => {
        await mountCard();
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ create: true } as never); });
        expect(await screen.findByRole('group', { name: 'What should this channel learn to do?' })).toBeInTheDocument();
    });

    it('with the context true a cell\'s scoped Manage tab keeps Copy to channel… and Publish to catalog… enabled (plan 1105 P7)', async () => {
        render(withReadOnly(true, <CapabilitiesManageBody scope={{ cellId: 'views' }} />));
        await screen.findByRole('article', { name: 'Memo handler' });
        for (const name of ['Copy to channel…', 'Publish to catalog…']) {
            const button = screen.getByRole('button', { name }) as HTMLButtonElement;
            expect(button.disabled).toBe(false);
            expect(button.closest('fieldset')).toBeNull();
        }
        expect(queryButton('Create…')).toBeNull();
    });
});
