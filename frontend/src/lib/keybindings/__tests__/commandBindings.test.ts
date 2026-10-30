// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';
import { useCommandBindings, type BindableCommand } from '../commandBindings';
import { isTypingTarget } from '../match';
import { useKeybindingsStore } from '../store';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

const press = (target: EventTarget, init: KeyboardEventInit) => {
    const e = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, composed: true, ...init });
    target.dispatchEvent(e);
    return e;
};

const field = (tag: 'input' | 'textarea' | 'select') => {
    const el = document.createElement(tag);
    document.body.append(el);
    return el;
};

/** Rows whose run records which selection it acted on. */
function rows(selection: { cell: string }, ran: string[]): BindableCommand[] {
    return [
        { key: 'run', shortcut: 'stack.run', run: () => ran.push(`run:${selection.cell}`) },
        { key: 'log', run: () => ran.push(`log:${selection.cell}`) },
        { key: 'files', run: () => ran.push(`files:${selection.cell}`) },
    ];
}

beforeEach(() => {
    useCollimateStore.setState({ commandPane: defaultCommandPane() } as never);
});
afterEach(() => {
    cleanup();
    document.body.replaceChildren();
    useKeybindingsStore.setState({ overrides: {}, commands: {}, capturing: null });
});

describe('useCommandBindings', () => {
    it('runs the bound row, records it as recent and takes the key (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        const e = press(document.body, { key: 'l', altKey: true });
        expect(ran).toEqual(['log:a']);
        expect(e.defaultPrevented).toBe(true);
        expect(useCollimateStore.getState().commandPane.recents).toEqual(['log']);
        const other = press(document.body, { key: 'l' });
        expect(ran).toEqual(['log:a']);
        expect(other.defaultPrevented).toBe(false);
    });

    it('builds the rows at keydown time, so a row acts on the current selection (CP-2)', () => {
        const ran: string[] = [];
        const selection = { cell: 'a' };
        useKeybindingsStore.getState().setCommandBinding('log', 'Mod+l');
        const { rerender } = renderHook(({ cell }) => useCommandBindings(() => rows({ cell }, ran)), { initialProps: selection });
        press(document.body, { key: 'l', ctrlKey: true });
        rerender({ cell: 'b' });
        press(document.body, { key: 'l', ctrlKey: true });
        expect(ran).toEqual(['log:a', 'log:b']);
    });

    it('a bare combo never fires from a text field; a modified chord fires from one (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.getState().setCommandBinding('log', 'g');
        useKeybindingsStore.getState().setCommandBinding('files', 'Alt+j');
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        for (const tag of ['input', 'textarea', 'select'] as const) {
            const el = field(tag);
            press(el, { key: 'g' });
            press(el, { key: 'j', altKey: true });
        }
        expect(ran).toEqual(['files:a', 'files:a', 'files:a']);
        press(document.body, { key: 'g' });
        expect(ran.at(-1)).toBe('log:a');
    });

    it('reads a text field inside an open shadow root as typing (CP-2)', () => {
        const host = document.createElement('div');
        document.body.append(host);
        const inner = document.createElement('input');
        host.attachShadow({ mode: 'open' }).append(inner);
        const seen: boolean[] = [];
        const listener = (e: Event) => seen.push(isTypingTarget(e));
        window.addEventListener('keydown', listener, true);
        press(inner, { key: 'g' });
        press(host, { key: 'g' });
        window.removeEventListener('keydown', listener, true);
        expect(seen).toEqual([true, false]);
    });

    it('ignores registry-backed rows: their shortcut ids fire elsewhere (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.setState({ commands: { run: 'Alt+q' } });
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        const e = press(document.body, { key: 'q', altKey: true });
        expect(ran).toEqual([]);
        expect(e.defaultPrevented).toBe(false);
    });

    it('stands back from a press a registry shortcut holds, so a clash runs exactly one action (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+r');
        useKeybindingsStore.getState().setCommandBinding('files', 'Escape');
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        const run = press(document.body, { key: 'r', code: 'KeyR', altKey: true });
        const back = press(document.body, { key: 'Escape' });
        expect(ran).toEqual([]);
        expect(run.defaultPrevented).toBe(false);
        expect(back.defaultPrevented).toBe(false);
        // Enter is a universal key of the list context only: a command
        // bound to it fires where no list has it.
        useKeybindingsStore.getState().setCommandBinding('files', 'Enter');
        press(document.body, { key: 'Enter' });
        expect(ran).toEqual(['files:a']);
    });

    it('runs the first bound row in the rows\' order when two share a combo (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.setState({ commands: { files: 'Alt+x', log: 'Alt+x' } });
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        press(document.body, { key: 'x', altKey: true });
        expect(ran).toEqual(['log:a']);
    });

    it('stays out of a keyboard owner, a modal, a press already taken and the binding capture (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        const owner = document.createElement('div');
        owner.setAttribute('data-owns-keyboard', '');
        const button = document.createElement('button');
        owner.append(button);
        document.body.append(owner);
        press(button, { key: 'l', altKey: true });

        const modal = document.createElement('div');
        modal.setAttribute('aria-modal', 'true');
        document.body.append(modal);
        press(document.body, { key: 'l', altKey: true });
        modal.remove();
        expect(ran).toEqual([]);

        useKeybindingsStore.getState().setCapturing('files');
        press(document.body, { key: 'l', altKey: true });
        expect(ran).toEqual([]);
        useKeybindingsStore.getState().setCapturing(null);
        press(document.body, { key: 'l', altKey: true });
        expect(ran).toEqual(['log:a']);
    });

    it('a press something took before the bindings is left alone (CP-2)', () => {
        const ran: string[] = [];
        const taker = vi.fn((e: Event) => e.preventDefault());
        window.addEventListener('keydown', taker, { capture: true });
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        press(document.body, { key: 'l', altKey: true });
        window.removeEventListener('keydown', taker, { capture: true });
        expect(taker).toHaveBeenCalledOnce();
        expect(ran).toEqual([]);
    });

    it('unmounting removes the listener (CP-2)', () => {
        const ran: string[] = [];
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        const { unmount } = renderHook(() => useCommandBindings(() => rows({ cell: 'a' }, ran)));
        unmount();
        press(document.body, { key: 'l', altKey: true });
        expect(ran).toEqual([]);
    });
});
