// Moving the selection onto the Capabilities manage card takes the keys out
// of the cell that held them (its native renderer, its pooled frame, or a
// parked frame that silently kept them), and the card's search takes them.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { CapabilitiesManageCard } from '../CapabilitiesManageCard';
import { useSelectionFocus } from '@/hooks/useSelectionFocus';
import { deepActiveElement } from '@/lib/blurForeignEditor';
import { frontCellOf } from '@/lib/frontSlots';
import { ACTIVE_TAB_BODY_ATTR, keyboardHolder } from '@/lib/selectionFocus';
import type { Stack } from '@/lib/types';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchHandlers } from '@/lib/definitionApi';
import { fetchRunnerGenerators } from '@/lib/runnerApi';
import {
    builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel,
} from './fixtures';

if (!globalThis.CSS?.escape) (globalThis as unknown as { CSS: { escape: (s: string) => string } }).CSS = { escape: (s: string) => s };

const pool = vi.hoisted(() => ({ containerOf: (() => null) as (el: Element) => HTMLElement | null }));
vi.mock('@/contexts/GuestPoolContext', async original => ({
    ...(await original<Record<string, unknown>>()),
    useGuestPool: () => ({ containerOf: pool.containerOf }),
}));
vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), addFromCatalog: vi.fn(),
}));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: [runner()], busy: false, error: null, refresh: async () => {} }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));

// Two stacks, as StackApp sees them.
const stackA: Stack = { id: 'sA', name: 'Work', cells: [{ id: 'A', name: 'notes.md', files: [] }] } as Stack;
const stackB: Stack = { id: 'sB', name: 'Other', cells: [{ id: 'B', name: 'other.md', files: [] }] } as Stack;
const caps: Stack = {
    id: 'caps', name: 'Capabilities', stack_type: 'capabilities',
    cells: [{ id: 'card', name: 'Definitions', files: [], builtin: true }],
} as Stack;
const STACKS = [stackA, stackB, caps];

// StackApp's wiring: selectedCellId = frontCellOf(selected column,
// vti); useSelectionFocus(selectedCellId, isZoomedOut); the manage card is
// active when the Capabilities stack is selected and slot 0 is its front.
function Harness({ selectedId, vti }: { selectedId: string; vti: Record<string, number> }) {
    const selectedColumn = STACKS.find(s => s.id === selectedId);
    const selectedFront = frontCellOf(selectedColumn, vti);
    const selectedCellId = selectedFront ? String(selectedFront.id) : null;
    useSelectionFocus(selectedCellId, false);
    const capsFront = (vti.caps ?? 0) === 0;
    return <CapabilitiesManageCard stackId="caps" width={480} height={640}
        isSelected={selectedId === 'caps'} isFront={capsFront}
        onSelect={() => {}} onShowCell={() => {}} onHide={() => {}} />;
}

/** A mounted cell card (outside React, like StackItem's DOM) with a native
 *  renderer (shadow textarea) in its focused tab. */
function nativeCell(id: string) {
    const shell = document.body.appendChild(document.createElement('div'));
    shell.setAttribute('data-cell-id', id);
    const card = shell.appendChild(document.createElement('div'));
    card.setAttribute('data-stack-card', '');
    const slot = card.appendChild(document.createElement('div'));
    slot.setAttribute(ACTIVE_TAB_BODY_ATTR, '');
    const wrapper = slot.appendChild(document.createElement('div'));
    wrapper.setAttribute('data-native-renderer', '');
    const host = wrapper.appendChild(document.createElement('div'));
    const editor = host.attachShadow({ mode: 'open' }).appendChild(document.createElement('textarea'));
    return { shell, card, slot, editor };
}

/** A cell whose editor is a pooled iframe in the surface overlay. */
function pooledCell(id: string, overlay: HTMLElement) {
    const shell = document.body.appendChild(document.createElement('div'));
    shell.setAttribute('data-cell-id', id);
    const card = shell.appendChild(document.createElement('div'));
    card.setAttribute('data-stack-card', '');
    const slot = card.appendChild(document.createElement('div'));
    slot.setAttribute(ACTIVE_TAB_BODY_ATTR, '');
    const container = slot.appendChild(document.createElement('div'));
    const frame = overlay.appendChild(document.createElement('iframe'));
    const realm = frame.contentWindow as unknown as { Element: typeof Element };
    vi.spyOn(realm.Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    const textarea = frame.contentDocument!.body.appendChild(frame.contentDocument!.createElement('textarea'));
    return { shell, card, slot, container, frame, textarea };
}

function surfaceOverlay() {
    const el = document.body.appendChild(document.createElement('div'));
    el.id = 'collimate-surface-overlay';
    return el;
}

let frames: FrameRequestCallback[] = [];
const flushFrames = (n = 3) => act(() => {
    for (let i = 0; i < n; i++) { const due = frames; frames = []; due.forEach(cb => cb(0)); }
});
const search = () => screen.getByRole('searchbox', { name: 'Search capabilities' });

beforeEach(() => {
    frames = [];
    pool.containerOf = () => null;
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    seedChannel();
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler(), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
});
afterEach(() => {
    (deepActiveElement(document) as HTMLElement | null)?.blur?.();
    cleanup();
    document.body.innerHTML = '';
    vi.restoreAllMocks();
});

describe('The selection moving onto the manage card', () => {
    it('between two ordinary cells the keys follow the selection (the rule the card relies on)', async () => {
        const a = nativeCell('A'), b = nativeCell('B');
        const view = render(<Harness selectedId="sA" vti={{}} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        flushFrames();
        a.editor.focus();
        view.rerender(<Harness selectedId="sB" vti={{}} />);
        flushFrames();
        expect(deepActiveElement(document)).toBe(b.editor);
    });

    it('from a native renderer\'s editor (Ctrl+→): the editor lets go and the search takes the keys', async () => {
        const a = nativeCell('A');
        const view = render(<Harness selectedId="sA" vti={{}} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        flushFrames();
        a.editor.focus();
        view.rerender(<Harness selectedId="caps" vti={{ caps: 0 }} />);
        // Nothing moves before the frame: a press that selected has its say.
        expect(deepActiveElement(document)).toBe(a.editor);
        flushFrames(3);
        expect(deepActiveElement(document)).not.toBe(a.editor);
        expect(search()).toHaveFocus();
    });

    it('from a pooled frame: the frame lets go and the search takes the keys', async () => {
        const over = surfaceOverlay();
        const a = pooledCell('A', over);
        pool.containerOf = el => (el === a.frame ? a.container : null);
        vi.spyOn(window, 'focus').mockImplementation(() => {});
        const view = render(<Harness selectedId="sA" vti={{}} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        flushFrames();
        a.frame.focus();
        a.textarea.focus();
        expect(document.activeElement).toBe(a.frame);
        view.rerender(<Harness selectedId="caps" vti={{ caps: 0 }} />);
        flushFrames(3);
        expect(keyboardHolder(document)).not.toBe(a.frame);
        expect(search()).toHaveFocus();
    });

    it('Alt+↑ onto the card from a cell whose parked frame silently kept the keys: the frame is released', async () => {
        const over = surfaceOverlay();
        const def = pooledCell('card', over);
        pool.containerOf = el => (el === def.frame ? def.container : null);
        const focusTop = vi.spyOn(window, 'focus').mockImplementation(() => {});
        const view = render(<Harness selectedId="caps" vti={{ caps: 1 }} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        flushFrames();
        def.frame.focus();
        // The pool parks the frame: the top document's activeElement drops to
        // the body, yet the frame keeps the keys.
        pool.containerOf = () => null;
        def.frame.blur();
        const silent = vi.spyOn(def.frame.contentDocument!, 'hasFocus').mockReturnValue(true);
        expect(keyboardHolder(document)).toBe(def.frame);
        view.rerender(<Harness selectedId="caps" vti={{ caps: 0 }} />);
        flushFrames(1);
        expect(focusTop).toHaveBeenCalled();
        silent.mockReturnValue(false);
        flushFrames(2);
        expect(search()).toHaveFocus();
    });

    it('leaves the keys where they are outside every cell (a pane\'s field)', async () => {
        const a = nativeCell('A');
        const view = render(<Harness selectedId="sA" vti={{}} />);
        await screen.findByRole('article', { name: 'Memo handler' });
        flushFrames();
        a.editor.focus();
        view.rerender(<Harness selectedId="caps" vti={{ caps: 0 }} />);
        const pane = document.body.appendChild(document.createElement('input'));
        pane.focus();
        flushFrames(3);
        expect(document.activeElement).toBe(pane);
    });
});
