import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, within, act } from '@testing-library/react';

import { AppTopBar } from './AppTopBar';
import { MARK_RAYS } from '@/lib/brandMark';
import { useTitleBarStore } from '@/stores/useTitleBarStore';
import { installDesktopShell, uninstallDesktopShell, LOCAL_SERVER_ENTRY } from '@/test/desktopShell';
import type { ServerEntry } from '@/lib/desktop';
import type { Channel, Stack, Cell } from '@/lib/types';
import { retiredOr } from '@/test/retiredTokens';

// The runs status is its own unit (C04); here it is only a mounted slot.
const runStatus = vi.hoisted(() => ({ props: null as null | { isZoomedOut: boolean; onExitZoom: () => void } }));
vi.mock('./RunStatusIndicator', () => ({
    RunStatusIndicator: (props: { isZoomedOut: boolean; onExitZoom: () => void }) => {
        runStatus.props = props;
        return <span data-testid="run-status" />;
    },
}));

const cell = (id: string): Cell => ({ id, name: id, files: [] });
const stack = (id: string, cells: Cell[], extra: Partial<Stack> = {}): Stack => ({ id, name: '', cells, ...extra });

const channel = (id = 'ch', name = 'chan'): Channel => ({
    id, name, nav_stacks: [], main_stacks: [], nav_selection_id: null, main_selection_id: null,
});

type Props = React.ComponentProps<typeof AppTopBar>;
const makeProps = (over: Partial<Props> = {}): Props => {
    const ch = over.activeChannel ?? channel();
    const noop = () => {};
    return {
        activeChannel: ch,
        channels: [ch],
        columns: [],
        selectedColumnId: null,
        visibleTopIndices: {},
        fanName: null,
        onSelectColumn: noop,
        isZoomedOut: false,
        toggleZoom: noop,
        capabilitiesStack: stack('cap', [], { stack_type: 'capabilities', name: 'Capabilities' }),
        capabilitiesShown: false,
        onToggleCapabilities: noop,
        onNewStack: noop,
        commandsOpen: false,
        onCommands: noop,
        onSelectChannel: noop,
        onSelectServerChannel: noop,
        onRequestNewChannel: noop,
        onRenameChannel: noop,
        onDeleteChannel: noop,
        onOpenSettings: noop,
        ...over,
    };
};

const bar = () => document.querySelector('[data-top-bar]') as HTMLElement;
const channelButton = () => screen.getByRole('button', { name: /chan/ });

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('AppTopBar — one 36px bar in the page flow', () => {
    it.each([false, true])('is one 3-column row, 36px, never laid over the scene (zoomed out: %s) (C01)', (isZoomedOut) => {
        render(<AppTopBar {...makeProps({ isZoomedOut })} />);
        const classes = bar().className.split(/\s+/);
        expect(classes).toEqual(expect.arrayContaining(['grid', 'h-9', 'grid-cols-[1fr_auto_1fr]', 'bg-canvas', 'border-b', 'border-line', 'text-[0.78125rem]']));
        expect(classes).not.toContain('absolute');
        expect(bar().children).toHaveLength(3);
    });

    it.each([false, true])('carries the same controls in both views, and no Settings, History, Stop all or search (zoomed out: %s) (C01)', (isZoomedOut) => {
        render(<AppTopBar {...makeProps({ isZoomedOut, columns: [stack('s1', [cell('a')])], selectedColumnId: 's1' })} />);
        for (const name of ['New stack', 'Commands', 'Capabilities', 'Previous cell', 'Next cell']) {
            expect(screen.queryByRole('button', { name })).not.toBeNull();
        }
        for (const gone of [/settings/i, /history/i, /stop all/i, /search cells/i, /new empty cell/i]) {
            expect(screen.queryByRole('button', { name: gone })).toBeNull();
        }
        expect(screen.getByTestId('run-status')).toBeTruthy();
    });

    it('lays the left column out as the mock: mark, # channel ▾, a separator, New stack, Commands (C01)', () => {
        render(<AppTopBar {...makeProps()} />);
        const left = bar().children[0] as HTMLElement;
        expect(left.firstElementChild?.className).toContain('text-brass');
        const buttons = within(left).getAllByRole('button');
        expect(buttons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual(['chan', 'New stack', 'Commands']);
        expect(left.querySelector('.w-px.bg-line')).not.toBeNull();
        // The right column: the runs slot, a separator, the Capabilities pill, the zoom toggle.
        const right = bar().children[2] as HTMLElement;
        expect(right.firstElementChild?.getAttribute('data-testid')).toBe('run-status');
        expect(within(right).getAllByRole('button').map(b => b.getAttribute('aria-label') ?? b.textContent))
            .toEqual(['Capabilities', 'Zoom out to the overview']);
    });

    it('New stack makes a stack; Commands toggles the pane and shows it open in teal without a fill (C01)', () => {
        const onNewStack = vi.fn();
        const onCommands = vi.fn();
        const { rerender } = render(<AppTopBar {...makeProps({ onNewStack, onCommands })} />);
        fireEvent.click(screen.getByRole('button', { name: 'New stack' }));
        expect(onNewStack).toHaveBeenCalledOnce();
        const commands = screen.getByRole('button', { name: 'Commands' });
        expect(commands.getAttribute('aria-pressed')).toBe('false');
        fireEvent.click(commands);
        expect(onCommands).toHaveBeenCalledOnce();
        rerender(<AppTopBar {...makeProps({ onNewStack, onCommands, commandsOpen: true })} />);
        expect(commands.getAttribute('aria-pressed')).toBe('true');
        expect(commands.className.split(/\s+/)).toContain('text-teal');
        expect(commands.className).not.toContain('aria-pressed:bg-sheet-2');
    });

    it('Commands names both of its keys; a right-click on New stack makes nothing (K01, C18)', () => {
        const onNewStack = vi.fn();
        render(<AppTopBar {...makeProps({ onNewStack })} />);
        const commands = screen.getByRole('button', { name: 'Commands' });
        expect(commands).toHaveAttribute('title', 'Commands · Ctrl+K toggles, / opens');
        expect(commands).toHaveAttribute('data-commands-toggle');
        const newStack = screen.getByRole('button', { name: 'New stack' });
        expect(fireEvent.contextMenu(newStack)).toBe(true);
        expect(onNewStack).not.toHaveBeenCalled();
    });

    it('the zoom toggle flips its icon, title and pressed state, and keeps its shortcut (C01)', () => {
        const toggleZoom = vi.fn();
        const { rerender } = render(<AppTopBar {...makeProps({ toggleZoom })} />);
        const zoom = screen.getByRole('button', { name: 'Zoom out to the overview' });
        expect(zoom.title).toBe('Zoom out to the overview');
        expect(zoom.getAttribute('aria-pressed')).toBe('false');
        expect(zoom.querySelector('svg')?.getAttribute('class')).toContain('lucide-layout-grid');
        fireEvent.click(zoom);
        expect(toggleZoom).toHaveBeenCalledOnce();
        rerender(<AppTopBar {...makeProps({ toggleZoom, isZoomedOut: true })} />);
        const back = screen.getByRole('button', { name: 'Zoom back in' });
        expect(back.title).toBe('Zoom back in');
        expect(back.getAttribute('aria-pressed')).toBe('true');
        expect(back.querySelector('svg')?.getAttribute('class')).toContain('lucide-shrink');
    });

    it('mounts the runs status with the view, and lets it leave the overview (C01)', () => {
        const toggleZoom = vi.fn();
        const { rerender } = render(<AppTopBar {...makeProps({ toggleZoom })} />);
        expect(runStatus.props!.isZoomedOut).toBe(false);
        runStatus.props!.onExitZoom();
        expect(toggleZoom).not.toHaveBeenCalled();
        rerender(<AppTopBar {...makeProps({ toggleZoom, isZoomedOut: true })} />);
        expect(runStatus.props!.isZoomedOut).toBe(true);
        runStatus.props!.onExitZoom();
        expect(toggleZoom).toHaveBeenCalledOnce();
    });

    it('the centre column is the channel scrubber over the display columns (C01)', () => {
        const onSelectColumn = vi.fn();
        const columns = [stack('s1', [cell('a')], { name: 'One' }), stack('s2', [cell('b')], { name: 'Two' })];
        render(<AppTopBar {...makeProps({ columns, selectedColumnId: 's1', onSelectColumn })} />);
        const centre = bar().children[1] as HTMLElement;
        expect(within(centre).getByRole('group', { name: 'Stacks in this channel' })).toBeTruthy();
        fireEvent.click(within(centre).getByLabelText('Two'));
        expect(onSelectColumn).toHaveBeenCalledWith('s2');
    });

    it.each([false, true])('carries no old token or colour literal (zoomed out: %s) (C01)', (isZoomedOut) => {
        const OLD = new RegExp(retiredOr('\\b(?:slate|zinc|gray|neutral|stone)-\\d', '\\b(?:bg|text|border)-(?:white|black)\\b', '#[0-9a-f]{3,8}\\b', 'rgba?\\(', 'hsla?\\(').source, 'i');
        const stacks = [stack('s1', [cell('a')])];
        const { container } = render(<AppTopBar {...makeProps({
            columns: stacks, selectedColumnId: 's1', isZoomedOut, commandsOpen: true,
            capabilitiesShown: true,
        })} />);
        fireEvent.click(channelButton());
        const all = [container, ...container.querySelectorAll('*'), ...document.querySelectorAll('[role="menu"] *')];
        expect(all.map(el => el.getAttribute('class') ?? '').filter(c => OLD.test(c))).toEqual([]);
    });
});

describe('AppTopBar — the brass mark toggles the desktop title bar row', () => {
    const TOGGLE = 'Title bar';
    const toggle = () => screen.getByRole('button', { name: TOGGLE });
    const hidden = () => useTitleBarStore.getState().titleBarHidden;

    afterEach(() => {
        cleanup();
        uninstallDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: false });
        localStorage.removeItem('collimate.titleBarHidden');
    });

    /** The left column's brand drawings: the mark's glyph (by its path)
     *  and any app-icon image. */
    const MARK_PATH = `path[d="${MARK_RAYS}"]`;
    const brand = () => {
        const left = bar().children[0] as HTMLElement;
        return {
            glyphs: [...left.querySelectorAll(MARK_PATH)].map(p => p.closest('svg') as SVGElement),
            images: left.querySelectorAll('img').length,
        };
    };

    it.each([false, true])('browser tab: the mark is inert, not a button, even with the hidden flag set (hidden: %s) (polish)', (flag) => {
        useTitleBarStore.setState({ titleBarHidden: flag });
        render(<AppTopBar {...makeProps()} />);
        expect(screen.queryByRole('button', { name: TOGGLE })).toBeNull();
        const { glyphs, images } = brand();
        expect(glyphs).toHaveLength(1);
        expect(glyphs[0].closest('button')).toBeNull();
        expect(glyphs[0].closest('.text-brass')).not.toBeNull();
        expect(images).toBe(0);
        fireEvent.click(glyphs[0]);
        expect(hidden()).toBe(flag);
    });

    it.each([false, true])('desktop: a click on the mark hides the row and a second click shows it (zoomed out: %s) (polish)', (isZoomedOut) => {
        installDesktopShell();
        render(<AppTopBar {...makeProps({ isZoomedOut })} />);
        expect(bar().children[0].firstElementChild).toBe(toggle());
        fireEvent.click(toggle());
        expect(hidden()).toBe(true);
        expect(localStorage.getItem('collimate.titleBarHidden')).toBe('1');
        expect(bar().children[0].firstElementChild).toBe(toggle());
        fireEvent.click(toggle());
        expect(hidden()).toBe(false);
        expect(localStorage.getItem('collimate.titleBarHidden')).toBe('0');
    });

    it('desktop: aria-pressed says whether the row is shown and the title names the click (polish)', () => {
        installDesktopShell();
        render(<AppTopBar {...makeProps()} />);
        expect(toggle().getAttribute('aria-pressed')).toBe('true');
        expect(toggle().title).toBe('Hide the title bar');
        fireEvent.click(toggle());
        expect(toggle().getAttribute('aria-pressed')).toBe('false');
        expect(toggle().title).toBe('Show the title bar');
        fireEvent.click(toggle());
        expect(toggle().getAttribute('aria-pressed')).toBe('true');
        expect(toggle().title).toBe('Hide the title bar');
    });

    it.each([false, true])('desktop: exactly one brand icon at the far left, the toggle, in both states (hidden: %s) (polish)', (flag) => {
        installDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: flag });
        render(<AppTopBar {...makeProps()} />);
        for (const state of [flag, !flag]) {
            expect(hidden()).toBe(state);
            const { glyphs, images } = brand();
            expect(glyphs).toHaveLength(1);
            expect(images).toBe(0);
            // The one glyph is the toggle's, not a separate mark beside it.
            expect(toggle().contains(glyphs[0])).toBe(true);
            expect(within(bar()).getAllByRole('button', { name: TOGGLE })).toHaveLength(1);
            fireEvent.click(toggle());
        }
    });

    it('desktop: the toggle keeps the brass, has the tools\' hover look and is a no-drag region (polish)', () => {
        installDesktopShell();
        render(<AppTopBar {...makeProps()} />);
        const btn = toggle();
        expect(btn.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-brass', 'size-[1.625rem]', 'rounded-md', 'transition-colors', 'hover:bg-sheet-2']));
        expect(btn.style.getPropertyValue('-webkit-app-region') || (btn.style as unknown as Record<string, string>).WebkitAppRegion).toBe('no-drag');
    });
});

describe('AppTopBar — the channel menu', () => {
    const channels = [channel('a', 'chan'), channel('b', 'bravo'), channel('c', 'charlie')];
    const open = (over: Partial<Props> = {}) => {
        render(<AppTopBar {...makeProps({ activeChannel: channels[0], channels, ...over })} />);
        fireEvent.click(channelButton());
        return screen.getByRole('menu', { name: 'Channels' });
    };

    it('the button shows # name ▾ and announces its menu (C02)', () => {
        render(<AppTopBar {...makeProps()} />);
        const btn = channelButton();
        expect(btn.textContent).toBe('chan');
        expect(btn.getAttribute('aria-haspopup')).toBe('menu');
        expect(btn.getAttribute('aria-expanded')).toBe('false');
        expect(btn.querySelector('.lucide-hash')).not.toBeNull();
        expect(btn.querySelector('.lucide-chevron-down')).not.toBeNull();
        fireEvent.click(btn);
        expect(btn.getAttribute('aria-expanded')).toBe('true');
    });

    it('lists channels as radio items, the current one checked and tagged active, then the operations, then Settings… after a break (C02, polish)', () => {
        const menu = open();
        const radios = within(menu).getAllByRole('menuitemradio');
        expect(radios.map(r => r.textContent)).toEqual(['chanactive', 'bravo', 'charlie']);
        expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false']);
        expect(within(radios[0]).getByText('active').className).toContain('text-brass');
        expect(within(menu).getAllByRole('separator')).toHaveLength(2);
        const items = within(menu).getAllByRole('menuitem');
        expect(items.map(i => i.querySelector('.truncate')?.textContent)).toEqual(['New channel…', 'Rename current…', 'Delete current…', 'Settings…']);
        expect(items[2].className).toContain('hover:text-red');
        expect(items[3].className).not.toContain('hover:text-red');
    });

    it('ends with a separator and Settings…, which opens Settings in the Commands pane and closes the menu (C02, polish)', () => {
        const onOpenSettings = vi.fn();
        const menu = open({ onOpenSettings });
        const rows = Array.from(menu.querySelectorAll('[role="separator"], [role="menuitem"], [role="menuitemradio"]'));
        const settings = within(menu).getByRole('menuitem', { name: /Settings…/ });
        expect(rows.at(-1)).toBe(settings);
        expect(rows.at(-2)?.getAttribute('role')).toBe('separator');
        expect(settings.querySelector('.lucide-settings')).not.toBeNull();
        fireEvent.click(settings);
        expect(onOpenSettings).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('focuses the first channel on open; the arrows move; Esc closes and refocuses the button (C02)', () => {
        const menu = open();
        const radios = within(menu).getAllByRole('menuitemradio');
        expect(document.activeElement).toBe(radios[0]);
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(radios[1]);
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
        expect(document.activeElement?.getAttribute('data-menu-item')).toBe('settings');
        fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
        expect(screen.queryByRole('menu')).toBeNull();
        expect(document.activeElement).toBe(channelButton());
    });

    it('picking a channel switches to it and closes (C02)', () => {
        const onSelectChannel = vi.fn();
        const menu = open({ onSelectChannel });
        fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'bravo' }));
        expect(onSelectChannel).toHaveBeenCalledWith('b');
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('an outside press closes it (C02)', () => {
        open();
        fireEvent.pointerDown(document.body);
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('New channel… opens the creation view (C02)', () => {
        const onRequestNewChannel = vi.fn();
        const menu = open({ onRequestNewChannel });
        fireEvent.click(within(menu).getByRole('menuitem', { name: 'New channel…' }));
        expect(onRequestNewChannel).toHaveBeenCalledOnce();
    });

    it('Rename current… opens the dialog, which owns the keyboard and renames on submit (C02)', () => {
        const onRenameChannel = vi.fn();
        const menu = open({ onRenameChannel });
        fireEvent.click(within(menu).getByRole('menuitem', { name: 'Rename current…' }));
        const input = document.activeElement as HTMLInputElement;
        expect(input.tagName).toBe('INPUT');
        expect(input.value).toBe('chan');
        expect(input.closest('[data-owns-keyboard]')).not.toBeNull();
        fireEvent.change(input, { target: { value: 'renamed' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        expect(onRenameChannel).toHaveBeenCalledWith('renamed');
        expect(screen.queryByText('Rename Channel')).toBeNull();
    });

    it('Delete current… deletes only once the name is typed (C02)', () => {
        const onDeleteChannel = vi.fn();
        const menu = open({ onDeleteChannel });
        fireEvent.click(within(menu).getByRole('menuitem', { name: 'Delete current…' }));
        const input = document.activeElement as HTMLInputElement;
        const confirm = screen.getByRole('button', { name: 'Delete' });
        fireEvent.change(input, { target: { value: 'nope' } });
        expect(confirm).toBeDisabled();
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onDeleteChannel).not.toHaveBeenCalled();
        fireEvent.change(input, { target: { value: 'chan' } });
        fireEvent.click(confirm);
        expect(onDeleteChannel).toHaveBeenCalledOnce();
    });

    it('opens without any network request and paints no per-channel tint (S06) (C02)', () => {
        const fetchSpy = vi.spyOn(globalThis, 'fetch');
        const menu = open();
        expect(within(menu).getByText('bravo')).toBeTruthy();
        expect(fetchSpy).not.toHaveBeenCalled();
        for (const svg of menu.querySelectorAll('svg')) expect(svg.getAttribute('style') ?? '').not.toMatch(/color/);
    });
});

describe('AppTopBar — the Capabilities pill', () => {
    const cap = stack('cap', [cell('c1')], { stack_type: 'capabilities', name: 'Capabilities' });
    const pill = () => screen.getByRole('button', { name: 'Capabilities' });

    it('is the one stack pill: a text pill, teal and pressed while the stack is on the canvas (C05)', () => {
        render(<AppTopBar {...makeProps({ capabilitiesStack: cap, capabilitiesShown: true })} />);
        expect(screen.queryByRole('button', { name: 'Generators' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Renderers' })).toBeNull();
        expect(pill().getAttribute('aria-pressed')).toBe('true');
        expect(pill().className.split(/\s+/)).toContain('text-teal');
        expect(pill().title).toBe('Capabilities: open on the canvas. Click to hide.');
        expect(pill().querySelector('svg')).toBeNull();
    });

    it('reads hidden, in ink, while the stack is hidden to the top bar (C05)', () => {
        render(<AppTopBar {...makeProps({ capabilitiesStack: cap, capabilitiesShown: false })} />);
        expect(pill().getAttribute('aria-pressed')).toBe('false');
        expect(pill().className.split(/\s+/)).toContain('text-ink-2');
        expect(pill().className.split(/\s+/)).not.toContain('text-teal');
        expect(pill().title).toBe('Capabilities: hidden. Click to show it as a stack.');
    });

    it.each([false, true])('a click toggles the stack (shown: %s) (C05)', (capabilitiesShown) => {
        const onToggleCapabilities = vi.fn();
        render(<AppTopBar {...makeProps({ capabilitiesStack: cap, capabilitiesShown, onToggleCapabilities })} />);
        fireEvent.click(pill());
        expect(onToggleCapabilities).toHaveBeenCalledOnce();
    });

    it('is not drawn before the channel has its stack (C05)', () => {
        render(<AppTopBar {...makeProps({ capabilitiesStack: null })} />);
        expect(screen.queryByRole('button', { name: 'Capabilities' })).toBeNull();
    });
});

describe('AppTopBar — the channel dialog', () => {
    beforeEach(() => cleanup());

    it('Esc cancels the rename dialog without renaming (C02)', () => {
        const onRenameChannel = vi.fn();
        render(<AppTopBar {...makeProps({ onRenameChannel })} />);
        fireEvent.click(channelButton());
        fireEvent.click(screen.getByRole('menuitem', { name: 'Rename current…' }));
        expect(screen.getByText('Rename Channel')).toBeTruthy();
        fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
        expect(screen.queryByText('Rename Channel')).toBeNull();
        expect(onRenameChannel).not.toHaveBeenCalled();
    });
});

describe('AppTopBar — channels on every server (desktop)', () => {
    const channels = [channel('a', 'chan'), channel('b', 'bravo')];
    const remote = (id: string, name: string, over: Partial<ServerEntry> = {}): ServerEntry => ({
        ...LOCAL_SERVER_ENTRY, id, kind: 'remote', name, url: `https://${id}.test`,
        hasToken: true, isWindowServer: false, channels: [], activeChannelId: null, ...over,
    });
    const catalog: ServerEntry[] = [
        { ...LOCAL_SERVER_ENTRY, name: 'This Mac', channels: [{ id: 'a', name: 'chan' }], activeChannelId: 'a' },
        remote('lab', 'Lab', { channels: [{ id: 'l1', name: 'Experiments' }, { id: 'l2', name: 'Notes' }], activeChannelId: 'l2' }),
        remote('off', 'Offline', { status: 'unreachable', channels: null, error: 'connection refused' }),
        remote('key', 'Keyed', { status: 'unauthorized', channels: null }),
        remote('empty', 'Empty'),
    ];
    const heads = (menu: HTMLElement) => [...menu.querySelectorAll('[role="presentation"]')].map(h => h.textContent);

    afterEach(() => { cleanup(); uninstallDesktopShell(); });

    const openDesktop = async (over: Partial<Props> = {}) => {
        const stub = installDesktopShell();
        vi.mocked(stub.shell.servers.list).mockResolvedValue(catalog);
        render(<AppTopBar {...makeProps({ activeChannel: channels[0], channels, ...over })} />);
        fireEvent.click(channelButton());
        const menu = screen.getByRole('menu', { name: 'Channels' });
        await within(menu).findByText('Experiments');
        return { stub, menu };
    };

    it('groups channels under one head per server, the window\'s first, with a status tag when a server cannot be read (MS-05)', async () => {
        const { stub, menu } = await openDesktop();
        // Listed on mount and again as the menu opened (the shell polls at once).
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(2);
        expect(heads(menu)).toEqual(['This Mac', 'Lab', 'Offline · unreachable', 'Keyed · unauthorized', 'Empty']);
        const radios = within(menu).getAllByRole('menuitemradio');
        // The window's server comes from the store's channels (live), the rest from the catalog.
        expect(radios.map(r => r.textContent)).toEqual(['chanactive', 'bravo', 'Experiments', 'Notes']);
        expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false', 'false']);
        expect(within(menu).getByRole('menuitem', { name: 'No channels' })).toHaveAttribute('aria-disabled', 'true');
    });

    it('a pick on another server calls onSelectServerChannel; one on the window\'s server stays onSelectChannel (MS-05)', async () => {
        const onSelectChannel = vi.fn();
        const onSelectServerChannel = vi.fn();
        const { menu } = await openDesktop({ onSelectChannel, onSelectServerChannel });
        fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'Notes' }));
        expect(onSelectServerChannel).toHaveBeenCalledExactlyOnceWith('lab', 'l2');
        expect(onSelectChannel).not.toHaveBeenCalled();
        expect(screen.queryByRole('menu')).toBeNull();

        fireEvent.click(channelButton());
        fireEvent.click(within(screen.getByRole('menu', { name: 'Channels' })).getByRole('menuitemradio', { name: 'bravo' }));
        expect(onSelectChannel).toHaveBeenCalledExactlyOnceWith('b');
        expect(onSelectServerChannel).toHaveBeenCalledOnce();
    });

    it('New, Rename and Delete stay scoped to the window\'s server (MS-05)', async () => {
        const onRequestNewChannel = vi.fn();
        const onRenameChannel = vi.fn();
        const onSelectServerChannel = vi.fn();
        const { menu } = await openDesktop({ onRequestNewChannel, onRenameChannel, onSelectServerChannel });
        const ops = within(menu).getAllByRole('menuitem').map(i => i.querySelector('.truncate')?.textContent);
        expect(ops).toEqual(['No channels', 'New channel…', 'Rename current…', 'Delete current…', 'Settings…']);
        fireEvent.click(within(menu).getByRole('menuitem', { name: 'Rename current…' }));
        const input = document.activeElement as HTMLInputElement;
        expect(input.value).toBe('chan');
        fireEvent.change(input, { target: { value: 'renamed' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        expect(onRenameChannel).toHaveBeenCalledExactlyOnceWith('renamed');
        fireEvent.click(channelButton());
        fireEvent.click(within(screen.getByRole('menu', { name: 'Channels' })).getByRole('menuitem', { name: 'New channel…' }));
        expect(onRequestNewChannel).toHaveBeenCalledOnce();
        expect(onSelectServerChannel).not.toHaveBeenCalled();
    });

    it('follows the shell\'s pushes while open (MS-05)', async () => {
        const { stub, menu } = await openDesktop();
        act(() => stub.emitServersChanged([catalog[0], { ...catalog[1], status: 'unreachable', channels: null }]));
        expect(heads(menu)).toEqual(['This Mac', 'Lab · unreachable']);
        expect(within(menu).queryByRole('menuitemradio', { name: 'Experiments' })).toBeNull();
    });

    it('a browser lists only this server\'s channels under one Channels head (MS-05)', () => {
        render(<AppTopBar {...makeProps({ activeChannel: channels[0], channels })} />);
        fireEvent.click(channelButton());
        const menu = screen.getByRole('menu', { name: 'Channels' });
        expect(heads(menu)).toEqual(['Channels']);
        expect(within(menu).getAllByRole('menuitemradio').map(r => r.textContent)).toEqual(['chanactive', 'bravo']);
    });
});
