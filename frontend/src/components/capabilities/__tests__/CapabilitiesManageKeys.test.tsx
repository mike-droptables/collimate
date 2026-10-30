// The manage card owns its bare keys only: the scene's chords (Alt+↑/↓
// paging, Ctrl+←/→, Mod+K, Alt+Shift+Backspace hiding the stack) still
// reach the real useKeyboardHandler from its search and its root, while '/'
// typed in the search stays there.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { useKeyboardHandler } from '@/hooks/useKeyboardHandler';
import type { FocusContextType } from '@/contexts/FocusContext';
import type { SceneActions } from '@/hooks/useSceneActions';
import { CapabilitiesManageCard } from '../CapabilitiesManageCard';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchHandlers } from '@/lib/definitionApi';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { fetchRunnerGenerators, type RunnerEntry } from '@/lib/runnerApi';
import type { Stack } from '@/lib/types';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), addFromCatalog: vi.fn(),
}));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[] | null }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: async () => {} }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));

const CAPS: Stack = {
    id: 'caps', name: 'Capabilities', stack_type: 'capabilities',
    cells: [{ id: 'defs', name: 'Definitions', files: [], builtin: true }, { id: 'u', name: 'Mine', files: [] }],
} as Stack;
const WORK: Stack = { id: 's2', name: 'Work', cells: [{ id: 'w', name: 'W', files: [] }] } as Stack;
const STACKS = [CAPS, WORK];

const focus = {
    focusArea: 'main', selectedWindowId: 'caps', mainFocusMode: 'window',
    setFocusArea: vi.fn(), setSelectedWindowId: vi.fn(), setMainFocusMode: vi.fn(),
} as unknown as FocusContextType;

/** The scene's keyboard with Capabilities selected and its manage card
 *  (slot 0) in front. */
function mountScene() {
    const actions = { winRotate: vi.fn(), newEmptyStackRight: vi.fn(async () => null) } as unknown as SceneActions & Record<string, ReturnType<typeof vi.fn>>;
    const handleRemoveStack = vi.fn();
    const selectDisplayId = vi.fn();
    renderHook(() => useKeyboardHandler({
        focusState: focus, mainStacks: STACKS, activeChannel: null,
        performAction: (p) => p, isZoomedOut: false, setIsZoomedOut: vi.fn(), toggleZoom: vi.fn(),
        setTransitionType: vi.fn(), setVisibleTopIndices: vi.fn(), setMergeNonces: vi.fn(),
        visibleTopIndicesRef: { current: { caps: 0 } }, handleRemoveStack, actions,
        fannedOutStackId: null, setFannedOutStackId: vi.fn(), fan: null,
        displayStacks: STACKS, displaySelectedId: 'caps', selectDisplayId,
    }));
    return { actions, handleRemoveStack, selectDisplayId };
}

const key = (target: EventTarget, init: KeyboardEventInit) => {
    target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, composed: true, ...init }));
};
/** Whether the scene sees the key as owned, read while it is dispatched
 *  (as the scene's window listener reads it). */
const owned = (target: EventTarget, init: KeyboardEventInit) => {
    let seen: boolean | null = null;
    const listen = (e: KeyboardEvent) => { seen = sceneKeyboardOwned(e); };
    window.addEventListener('keydown', listen, true);
    try { key(target, init); } finally { window.removeEventListener('keydown', listen, true); }
    return seen;
};
const search = () => screen.getByRole('searchbox', { name: 'Search capabilities' });

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
});
afterEach(cleanup);

async function mountCard() {
    (document.activeElement as HTMLElement | null)?.blur?.();
    const view = render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront
        onSelect={vi.fn()} onShowCell={vi.fn()} onHide={vi.fn()} />);
    await screen.findByRole('article', { name: 'Memo handler' });
    return view.container.querySelector<HTMLElement>('[data-capabilities-manage]')!;
}

describe('The manage card and the scene keyboard', () => {
    it('Alt+↓ from the focused search pages the stack', async () => {
        const { actions } = mountScene();
        await mountCard();
        expect(search()).toHaveFocus();
        key(search(), { key: 'ArrowDown', code: 'ArrowDown', altKey: true });
        expect(actions.winRotate).toHaveBeenCalledWith('caps', 1);
    });

    it('Alt+↓ from the card root pages the stack, and Ctrl+→ is the scene’s', async () => {
        const { actions } = mountScene();
        const root = await mountCard();
        act(() => { root.focus(); });
        expect(root).toHaveFocus();
        key(root, { key: 'ArrowDown', code: 'ArrowDown', altKey: true });
        expect(actions.winRotate).toHaveBeenCalledWith('caps', 1);
        expect(owned(root, { key: 'ArrowRight', code: 'ArrowRight', ctrlKey: true })).toBe(false);
        expect(owned(search(), { key: 'k', code: 'KeyK', ctrlKey: true })).toBe(false);
        expect(owned(search(), { key: 'k', code: 'KeyK', metaKey: true })).toBe(false);
    });

    it('Alt+Shift+Backspace from the card hides the stack', async () => {
        const { handleRemoveStack } = mountScene();
        await mountCard();
        key(search(), { key: 'Backspace', code: 'Backspace', altKey: true, shiftKey: true });
        expect(handleRemoveStack).toHaveBeenCalledWith('caps');
    });

    it('keeps its bare keys: ‘/’, plain arrows, Enter, Esc and Shift-only keys never reach the scene', async () => {
        const { actions, handleRemoveStack } = mountScene();
        const root = await mountCard();
        for (const init of [{ key: '/' }, { key: '?', shiftKey: true }, { key: 'ArrowDown' }, { key: 'Enter' }, { key: 'Escape' }, { key: 'Backspace' }, { key: 'a' }]) {
            expect(owned(search(), init)).toBe(true);
        }
        act(() => { root.focus(); });
        expect(owned(root, { key: '/' })).toBe(true);
        expect(owned(root, { key: 'ArrowUp' })).toBe(true);
        expect(actions.winRotate).not.toHaveBeenCalled();
        expect(handleRemoveStack).not.toHaveBeenCalled();
    });
});
