import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useRef, useState } from 'react';
import { Hash, Plus, Trash2 } from 'lucide-react';
import { Menu, type MenuCloseReason, type MenuEntry } from './Menu';
import { isCellOccluded, resetGuestOcclusion } from '@/lib/guestOcclusion';
import { KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { useKeybindingsStore } from '@/lib/keybindings/store';

// A channel-menu-shaped list: a head, three radio rows (the current one
// tagged), a separator, two items, a red item, and one inert row.
const entries = (onSelect = vi.fn()): MenuEntry[] => [
    { kind: 'head', id: 'h', label: 'Channels' },
    { kind: 'radio', id: 'a', label: 'alpha', icon: Hash, checked: true, tag: 'active', onSelect },
    { kind: 'radio', id: 'b', label: 'beta', icon: Hash, checked: false, onSelect },
    { kind: 'separator', id: 's' },
    { kind: 'item', id: 'new', label: 'New channel…', icon: Plus, hint: 'Make a channel', onSelect },
    { kind: 'item', id: 'off', label: 'Inert', disabled: true, reason: 'Not now', onSelect },
    { kind: 'item', id: 'del', label: 'Delete current…', icon: Trash2, danger: true, onSelect },
];

function Harness({ items, onClose, role = 'menu', children, align, minWidth }: {
    items?: MenuEntry[];
    onClose?: (r: MenuCloseReason) => void;
    role?: 'menu' | 'dialog';
    children?: React.ReactNode;
    align?: 'start' | 'end';
    minWidth?: number;
}) {
    const ref = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    return (
        <>
            <button ref={ref} type="button" onClick={() => setOpen(o => !o)}>anchor</button>
            <button type="button">elsewhere</button>
            <Menu
                anchor={ref}
                open={open}
                role={role}
                label="Channels"
                items={items}
                align={align}
                minWidth={minWidth}
                onClose={(r) => { setOpen(false); onClose?.(r); }}
            >
                {children}
            </Menu>
        </>
    );
}

const anchor = () => screen.getByRole('button', { name: 'anchor' });
const openIt = (role: 'menu' | 'dialog' = 'menu') => {
    fireEvent.click(anchor());
    return screen.getByRole(role);
};
const row = (name: string) => screen.getByText(name).closest('button') as HTMLButtonElement;

beforeEach(() => { cleanup(); resetGuestOcclusion(); });
afterEach(() => { vi.restoreAllMocks(); });

describe('ui/Menu', () => {
    it('renders roles: menu, menuitemradio with aria-checked, menuitem, separator, a head (C07)', () => {
        render(<Harness items={entries()} />);
        expect(screen.queryByRole('menu')).toBeNull();
        const menu = openIt();
        expect(menu.getAttribute('aria-label')).toBe('Channels');
        const radios = within(menu).getAllByRole('menuitemradio');
        expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['true', 'false']);
        expect(within(menu).getAllByRole('menuitem').map(r => r.textContent)).toEqual(['New channel…', 'Inert', 'Delete current…']);
        expect(within(menu).getByRole('separator').className).toContain('bg-line');
        expect(within(menu).getByText('Channels').className).toContain('uppercase');
        // The current choice: ink text, brass icon, the brass tag.
        expect(radios[0].className).toContain('[&_svg]:text-brass');
        expect(within(radios[0]).getByText('active').className).toContain('text-brass');
        // The mock's .menu: 248px by default, shared surface classes, 30px rows.
        expect(menu.style.minWidth).toBe('248px');
        expect(menu.className.split(/\s+/)).toEqual(expect.arrayContaining(['bg-sheet', 'border-line-2', 'shadow-(--shadow)', 'p-1']));
        expect(radios[1].className).toContain('h-[1.875rem]');
        expect(row('Delete current…').className).toContain('hover:text-red');
        expect(row('Inert').getAttribute('aria-disabled')).toBe('true');
        expect(row('Inert').title).toBe('Not now');
        expect(row('New channel…').title).toBe('Make a channel');
    });

    it('is portalled to body, owns the keyboard and freezes native guests while open (C07)', () => {
        const { container } = render(<Harness items={entries()} />);
        expect(isCellOccluded('any')).toBe(false);
        const menu = openIt();
        expect(container.contains(menu)).toBe(false);
        expect(menu.parentElement).toBe(document.body);
        expect(menu.hasAttribute(KEYBOARD_OWNER_ATTR)).toBe(true);
        expect(isCellOccluded('any')).toBe(true);
        fireEvent.keyDown(menu, { key: 'Escape' });
        expect(isCellOccluded('any')).toBe(false);
    });

    it('focuses the first live row; arrows wrap over rows only, Home / End jump (C07)', () => {
        render(<Harness items={entries()} />);
        const menu = openIt();
        expect(document.activeElement).toBe(row('alpha'));
        fireEvent.keyDown(menu, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(row('Delete current…'));
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(row('alpha'));
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        // The separator is skipped; the inert row is reachable (its reason is its title).
        expect(document.activeElement).toBe(row('New channel…'));
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(row('Inert'));
        fireEvent.keyDown(menu, { key: 'Home' });
        expect(document.activeElement).toBe(row('alpha'));
        fireEvent.keyDown(menu, { key: 'End' });
        expect(document.activeElement).toBe(row('Delete current…'));
    });

    it('the first live row takes focus when the list opens on an inert one (C07)', () => {
        const onSelect = vi.fn();
        render(<Harness items={[
            { kind: 'item', id: 'x', label: 'Off', disabled: true, reason: 'r', onSelect },
            { kind: 'item', id: 'y', label: 'On', onSelect },
        ]} />);
        openIt();
        expect(document.activeElement).toBe(row('On'));
    });

    it('Enter and Space select, close with reason select and hand focus back to the anchor (C07)', () => {
        const onSelect = vi.fn();
        const onClose = vi.fn();
        render(<Harness items={entries(onSelect)} onClose={onClose} />);
        let menu = openIt();
        fireEvent.keyDown(menu, { key: 'Enter' });
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onClose).toHaveBeenLastCalledWith('select');
        expect(screen.queryByRole('menu')).toBeNull();
        expect(document.activeElement).toBe(anchor());

        menu = openIt();
        fireEvent.keyDown(menu, { key: 'End' });
        fireEvent.keyDown(menu, { key: ' ' });
        expect(onSelect).toHaveBeenCalledTimes(2);
    });

    it('an inert row does nothing on click or Enter and keeps the menu open (C07)', () => {
        const onSelect = vi.fn();
        render(<Harness items={entries(onSelect)} />);
        const menu = openIt();
        fireEvent.click(row('Inert'));
        row('Inert').focus();
        fireEvent.keyDown(menu, { key: 'Enter' });
        expect(onSelect).not.toHaveBeenCalled();
        expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('Esc closes, refocuses the anchor and never reaches window listeners (C07)', () => {
        const onClose = vi.fn();
        render(<Harness items={entries()} onClose={onClose} />);
        const menu = openIt();
        const bubble = vi.fn();
        window.addEventListener('keydown', bubble);
        fireEvent.keyDown(menu, { key: 'Escape' });
        window.removeEventListener('keydown', bubble);
        expect(bubble).not.toHaveBeenCalled();
        expect(onClose).toHaveBeenCalledWith('escape');
        expect(document.activeElement).toBe(anchor());
    });

    it('Tab closes a menu with focus on the anchor, so the Tab moves on from there (C07)', () => {
        const onClose = vi.fn();
        render(<Harness items={entries()} onClose={onClose} />);
        fireEvent.keyDown(openIt(), { key: 'Tab' });
        expect(onClose).toHaveBeenCalledWith('tab');
        expect(screen.queryByRole('menu')).toBeNull();
        expect(document.activeElement).toBe(anchor());
    });

    it('closes on an outside press and on window blur; the anchor press is exempt so its click toggles (C07)', () => {
        const onClose = vi.fn();
        render(<Harness items={entries()} onClose={onClose} />);
        openIt();
        fireEvent.pointerDown(screen.getByRole('button', { name: 'elsewhere' }));
        expect(onClose).toHaveBeenLastCalledWith('outside');
        expect(screen.queryByRole('menu')).toBeNull();

        openIt();
        fireEvent.blur(window);
        expect(onClose).toHaveBeenLastCalledWith('blur');

        const menu = openIt();
        fireEvent.pointerDown(menu);
        expect(screen.getByRole('menu')).toBeTruthy();
        fireEvent.pointerDown(anchor());
        expect(screen.getByRole('menu')).toBeTruthy();
        fireEvent.click(anchor());
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('closes when the anchor moves under the camera, or a rect anchor goes away (C07)', () => {
        const frames: FrameRequestCallback[] = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
        const flush = () => act(() => { frames.splice(0).forEach(f => f(0)); });

        const onClose = vi.fn();
        render(<Harness items={entries()} onClose={onClose} />);
        let x = 100;
        vi.spyOn(anchor(), 'getBoundingClientRect').mockImplementation(() => new DOMRect(x, 20, 40, 26));
        openIt();
        flush();
        flush();
        expect(screen.getByRole('menu')).toBeTruthy();
        x = 140;
        flush();
        expect(onClose).toHaveBeenLastCalledWith('anchor');
        expect(screen.queryByRole('menu')).toBeNull();
        cleanup();

        let rect: DOMRect | null = new DOMRect(10, 10, 20, 20);
        const close = vi.fn();
        render(<Menu anchor={() => rect} open label="R" items={entries()} onClose={close} />);
        flush();
        expect(close).not.toHaveBeenCalled();
        rect = null;
        flush();
        expect(close).toHaveBeenCalledWith('anchor');
    });

    it('an end-aligned menu stays open when its anchor grows leftward; its own right edge moving closes it (C04)', () => {
        const frames: FrameRequestCallback[] = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
        const flush = () => act(() => { frames.splice(0).forEach(f => f(0)); });

        const onClose = vi.fn();
        render(<Harness items={entries()} onClose={onClose} align="end" />);
        // Right edge fixed at 200: the label loses a digit and its left moves.
        let left = 120;
        vi.spyOn(anchor(), 'getBoundingClientRect').mockImplementation(() => new DOMRect(left, 20, 200 - left, 26));
        openIt();
        flush();
        left = 128;
        flush();
        flush();
        expect(onClose).not.toHaveBeenCalled();
        expect(screen.getByRole('menu')).toBeTruthy();
        cleanup();

        // A start-aligned menu watches the left edge instead.
        const onCloseStart = vi.fn();
        render(<Harness items={entries()} onClose={onCloseStart} align="start" />);
        let width = 80;
        vi.spyOn(anchor(), 'getBoundingClientRect').mockImplementation(() => new DOMRect(40, 20, width, 26));
        openIt();
        flush();
        width = 96;
        flush();
        expect(onCloseStart).not.toHaveBeenCalled();
        vi.spyOn(anchor(), 'getBoundingClientRect').mockImplementation(() => new DOMRect(48, 20, width, 26));
        flush();
        expect(onCloseStart).toHaveBeenLastCalledWith('anchor');
    });

    it('caps its height to the room on the chosen side and scrolls; flips above only when that side is larger (C02)', () => {
        vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(248);
        vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(900);
        vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(400);
        render(<Harness items={entries()} align="start" />);
        // Under a top-bar button: 400 - 32 - 5 - 8 = 355 below, almost nothing above.
        vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(72, 6, 120, 26));
        let menu = openIt();
        expect(menu.style.top).toBe('37px');
        expect(menu.style.maxHeight).toBe('355px');
        expect(menu.className.split(/\s+/)).toContain('overflow-y-auto');
        cleanup();

        // Near the bottom: more room above, so it flips and caps to that room.
        render(<Harness items={entries()} align="start" />);
        vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(72, 300, 120, 26));
        menu = openIt();
        expect(menu.style.maxHeight).toBe(`${300 - 5 - 8}px`);
        expect(menu.style.top).toBe('8px');
    });

    it('a press inside the menu never reaches its owner\'s React ancestors (C17)', () => {
        const ownerDown = vi.fn();
        render(<div onPointerDown={ownerDown}><Harness items={entries()} /></div>);
        openIt();
        ownerDown.mockClear();
        fireEvent.pointerDown(row('beta'));
        expect(ownerDown).not.toHaveBeenCalled();
        expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('places under the anchor: right edges aligned for end, left edges for start, 5px below (C07)', () => {
        vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(236);
        vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(200);
        render(<Harness items={entries()} align="end" />);
        vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(500, 40, 22, 22));
        let menu = openIt();
        expect(menu.style.left).toBe(`${522 - 236}px`);
        expect(menu.style.top).toBe(`${62 + 5}px`);
        cleanup();

        render(<Harness items={entries()} align="start" />);
        vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(72, 6, 120, 26));
        menu = openIt();
        expect(menu.style.left).toBe('72px');
        expect(menu.style.top).toBe(`${32 + 5}px`);
    });

    it('role=dialog hosts its own body: first control focused, its arrows left alone, Tab closes past the end (C07)', () => {
        const onClose = vi.fn();
        const bodyKeys = vi.fn();
        render(
            <Harness role="dialog" onClose={onClose}>
                <div onKeyDown={bodyKeys}>
                    <button type="button">run one</button>
                    <button type="button">Stop all 2</button>
                </div>
            </Harness>,
        );
        const dialog = openIt('dialog');
        expect(dialog.getAttribute('aria-label')).toBe('Channels');
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'run one' }));
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        expect(bodyKeys).toHaveBeenCalled();
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'run one' }));
        fireEvent.keyDown(document.activeElement!, { key: 'Tab' });
        expect(screen.getByRole('dialog')).toBeTruthy();
        screen.getByRole('button', { name: 'Stop all 2' }).focus();
        fireEvent.keyDown(document.activeElement!, { key: 'Tab' });
        expect(onClose).toHaveBeenCalledWith('tab');
        expect(document.activeElement).toBe(anchor());
    });
    it('hangs from an element anchor and shows a row\'s live shortcut, nothing for an unbound one (C07)', () => {
        const resolveString = useKeybindingsStore.getState().resolveString;
        useKeybindingsStore.setState({
            resolveString: (id) => (id === SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM ? '' : resolveString(id)),
        });
        try {
            const el = document.createElement('button');
            document.body.appendChild(el);
            const onSelect = vi.fn();
            render(<Menu anchor={el} open label="Element" onClose={vi.fn()} items={[
                { kind: 'item', id: 'p', label: 'Palette', shortcut: SHORTCUT_IDS.GLOBAL_OPEN_PALETTE, onSelect },
                { kind: 'item', id: 'z', label: 'Zoom', shortcut: SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM, onSelect },
            ]} />);
            expect(screen.getByRole('menu')).toBeTruthy();
            expect(row('Palette').querySelector('.font-mono')?.textContent).toBeTruthy();
            expect(row('Zoom').querySelector('.font-mono')).toBeNull();
            fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
            expect(document.activeElement).toBe(el);
            el.remove();
        } finally {
            useKeybindingsStore.setState({ resolveString });
        }
    });

    it('keys it does not own pass through; Enter on the surface is inert; ArrowUp from the surface lands on the last row (C07)', () => {
        const onSelect = vi.fn();
        render(<Harness items={entries(onSelect)} />);
        const menu = openIt();
        const other = new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true });
        menu.dispatchEvent(other);
        expect(other.defaultPrevented).toBe(false);
        menu.focus();
        fireEvent.keyDown(menu, { key: 'Enter' });
        expect(onSelect).not.toHaveBeenCalled();
        expect(screen.getByRole('menu')).toBeTruthy();
        fireEvent.keyDown(menu, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(row('Delete current…'));
        // A key its owner already handled is left alone.
        const handled = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
        handled.preventDefault();
        menu.dispatchEvent(handled);
        expect(screen.getByRole('menu')).toBeTruthy();
    });

    it('an empty menu takes focus itself and its arrows do nothing (C07)', () => {
        render(<Harness items={[]} />);
        const menu = openIt();
        expect(document.activeElement).toBe(menu);
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(menu);
    });

    it('role=dialog: its body keeps ArrowUp, Home and End; Shift+Tab past the first control closes; rows take the arrows (C07)', () => {
        const onClose = vi.fn();
        const onSelect = vi.fn();
        render(
            <Harness role="dialog" onClose={onClose} items={[
                { kind: 'item', id: 'r1', label: 'Row one', onSelect },
                { kind: 'item', id: 'r2', label: 'Row two', onSelect },
            ]}>
                <button type="button">body control</button>
            </Harness>,
        );
        openIt('dialog');
        expect(document.activeElement).toBe(row('Row one'));
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(row('Row two'));
        const body = screen.getByRole('button', { name: 'body control' });
        body.focus();
        for (const key of ['ArrowUp', 'Home', 'End', 'ArrowDown']) {
            fireEvent.keyDown(body, { key });
            expect(document.activeElement).toBe(body);
        }
        fireEvent.keyDown(body, { key: 'Tab', shiftKey: true });
        expect(screen.getByRole('dialog')).toBeTruthy();
        expect(onClose).not.toHaveBeenCalled();
        cleanup();

        // A body with nothing focusable: Tab leaves at once.
        const onCloseEmpty = vi.fn();
        render(<Harness role="dialog" onClose={onCloseEmpty}><p>Nothing running</p></Harness>);
        const dialog = openIt('dialog');
        expect(document.activeElement).toBe(dialog);
        fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
        expect(onCloseEmpty).toHaveBeenCalledWith('tab');
    });

    it('a rect anchor that is already gone closes it at once (C07)', () => {
        const close = vi.fn();
        render(<Menu anchor={() => null} open label="Gone" items={entries()} onClose={close} />);
        expect(close).toHaveBeenCalledWith('anchor');
    });

    it('re-places when its body resizes, keeping its spot when nothing moved; places once without a ResizeObserver (C07)', () => {
        const observers: { cb: () => void; disconnect: ReturnType<typeof vi.fn> }[] = [];
        const Original = globalThis.ResizeObserver;
        class RO {
            cb: () => void;
            disconnect = vi.fn();
            constructor(cb: () => void) { this.cb = cb; observers.push(this); }
            observe() {}
            unobserve() {}
        }
        globalThis.ResizeObserver = RO as unknown as typeof ResizeObserver;
        try {
            let height = 100;
            vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(248);
            vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height);
            vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(400);
            const { unmount } = render(<Harness items={entries()} align="start" />);
            // 111px below the anchor, 237 above: 100px fits under it.
            vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(72, 250, 120, 26));
            const menu = openIt();
            expect(menu.style.top).toBe('281px');
            act(() => observers[0].cb());
            expect(menu.style.top).toBe('281px');
            // Grown to 300px it no longer fits below and flips, capped to the room above.
            height = 300;
            act(() => observers[0].cb());
            expect(menu.style.maxHeight).toBe('237px');
            expect(menu.style.top).toBe('8px');
            unmount();
            expect(observers[0].disconnect).toHaveBeenCalled();
        } finally {
            globalThis.ResizeObserver = Original;
        }

        delete (globalThis as { ResizeObserver?: unknown }).ResizeObserver;
        try {
            render(<Harness items={entries()} align="start" />);
            vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(72, 6, 120, 26));
            expect(openIt().style.left).toBe('72px');
        } finally {
            globalThis.ResizeObserver = Original;
        }
    });

    it('an end-aligned menu that flips above grows from its bottom-right corner (C02)', () => {
        vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(248);
        vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(900);
        vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(400);
        render(<Harness items={entries()} align="end" />);
        vi.spyOn(anchor(), 'getBoundingClientRect').mockReturnValue(new DOMRect(300, 300, 120, 26));
        const menu = openIt();
        expect(menu.className.split(/\s+/)).toContain('origin-bottom-right');
    });
});
