import React, { useLayoutEffect, useRef, useState } from 'react';
import { ChevronDown, Hash, LayoutGrid, Pencil, Plus, Search, Settings, Shrink, Trash2 } from 'lucide-react';
import type { Channel, Stack } from '@/lib/types';
import { cn } from '@/lib/utils';
import { useShortcutDisplay, useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { COMMANDS_TOGGLE_ATTR } from '@/lib/commandPane';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { Menu, type MenuEntry } from '@/components/ui/Menu';
import { TopBarMark } from './DesktopTitleBar';
import { RunStatusIndicator } from './RunStatusIndicator';
import { ChannelDialog } from './ChannelDialog';
import { ChannelScrubber } from './ChannelScrubber';
import { desktopBridge, type ServerEntry, type ServerStatus } from '@/lib/desktop';
import { useServerCatalog } from '@/hooks/useServerCatalog';

// The app's one top bar (the mock's `.topbar`): 36px, in the page flow, the
// same row in the main view and in zoom-out. Three columns: the channel and
// creation on the left (the mark, `# channel ▾`, New stack, Commands), the
// channel scrubber in the centre, and on the right the running status, the
// Capabilities pill and the zoom toggle. Settings sits at the end
// of the channel menu, after a break (and in the Commands pane, and on
// Mod+,); Stop all lives in the runs menu.
//
// In the desktop shell the channel menu lists every server the shell knows,
// one head per server, the window's own first. The window shows one server
// at a time, so a channel on another server is opened by the shell, which
// selects it there and reopens the window on that server; New / Rename /
// Delete act on the window's server only.

interface AppTopBarProps {
    activeChannel: Channel;
    channels: Channel[];
    /** The display columns (a fanned stack as its columns), left to right. */
    columns: Stack[];
    /** The display column holding the selection. */
    selectedColumnId: string | null;
    visibleTopIndices: Record<string, number>;
    /** The fanned stack's name, when one is fanned. */
    fanName: string | null;
    onSelectColumn: (columnId: string) => void;
    isZoomedOut: boolean;
    toggleZoom: () => void;
    /** The channel's Capabilities stack (null only before the channel loads). */
    capabilitiesStack: Stack | null;
    /** Whether Capabilities is on the canvas (else hidden to the top bar). */
    capabilitiesShown: boolean;
    /** Hide Capabilities, or show it as a stack right of the selection. */
    onToggleCapabilities: () => void;
    /** A new empty cell in its own stack, right of the selection. */
    onNewStack: () => void;
    /** Whether the Commands pane is open (the button turns teal). */
    commandsOpen: boolean;
    /** Toggle the Commands pane. */
    onCommands: () => void;
    onSelectChannel: (id: string) => void | Promise<void>;
    /** A channel on another server (desktop shell): the shell reopens the
     *  window on that server with the channel selected. */
    onSelectServerChannel: (serverId: string, channelId: string) => void;
    /** Open the channel-creation view. */
    onRequestNewChannel: () => void;
    onRenameChannel: (name: string) => void;
    onDeleteChannel: () => void;
    /** Open the Commands pane on its settings view (the channel menu's last
     *  item; Mod+,). */
    onOpenSettings: () => void;
}

/** A top-bar button (the mock's `.tb-btn`): 26px, no edge, the sheet fill on
 *  hover and while it is pressed or its menu is open. */
const tbBtn = 'inline-flex flex-none items-center gap-1.5 h-[1.625rem] px-2 rounded-md text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink';
const tbBtnOn = 'aria-pressed:bg-sheet-2 aria-pressed:text-ink aria-expanded:bg-sheet-2 aria-expanded:text-ink';
/** The Capabilities pill (the mock's `.sstack`): teal while the stack is on the canvas. */
const pill = 'inline-flex flex-none items-center h-[1.625rem] px-[0.5625rem] rounded-md font-medium transition-colors';

const Sep = () => <span aria-hidden className="w-px h-3.5 mx-1.5 flex-none bg-line" />;

/** The head's tag for a server whose channels cannot be listed. */
const STATUS_TAG: Record<ServerStatus, string> = {
    running: 'channels unavailable',
    starting: 'starting',
    stopped: 'stopped',
    unreachable: 'unreachable',
    unauthorized: 'unauthorized',
    mismatch: 'version mismatch',
    failed: 'failed',
};

/** The channel menu's channel rows: one list in a browser; in the desktop
 *  shell one group per server, the window's own first (from the store's
 *  channels, live), then the others from the shell's catalog. */
function channelRows({ channels, activeChannel, catalog, onSelectChannel, onSelectServerChannel }: {
    channels: Channel[];
    activeChannel: Channel;
    catalog: ServerEntry[] | null;
    onSelectChannel: (id: string) => void | Promise<void>;
    onSelectServerChannel: (serverId: string, channelId: string) => void;
}): MenuEntry[] {
    const own = channels.map((c): MenuEntry => ({
        kind: 'radio', id: `channel:${c.id}`, label: c.name, icon: Hash,
        checked: c.id === activeChannel.id,
        tag: c.id === activeChannel.id ? 'active' : undefined,
        onSelect: () => onSelectChannel(c.id),
    }));
    if (!catalog) return [{ kind: 'head', id: 'head', label: 'Channels' }, ...own];

    const windowServer = desktopBridge()?.server;
    const windowName = catalog.find(e => e.isWindowServer)?.name ?? windowServer?.name ?? 'This server';
    const rows: MenuEntry[] = [{ kind: 'head', id: 'head', label: windowName }, ...own];
    for (const entry of catalog) {
        if (entry.isWindowServer) continue;
        const listed = entry.status === 'running' && entry.channels !== null;
        rows.push({
            kind: 'head',
            id: `head:${entry.id}`,
            label: listed ? entry.name : `${entry.name} · ${STATUS_TAG[entry.status]}`,
        });
        if (!listed) continue;
        const serverChannels = entry.channels ?? [];
        if (serverChannels.length === 0) {
            rows.push({
                kind: 'item', id: `none:${entry.id}`, label: 'No channels', disabled: true,
                reason: `${entry.name} has no channels yet`, onSelect: () => {},
            });
            continue;
        }
        for (const c of serverChannels) {
            rows.push({
                kind: 'radio', id: `channel:${entry.id}:${c.id}`, label: c.name, icon: Hash,
                checked: false,
                hint: `Reopen the window on ${entry.name}`,
                onSelect: () => onSelectServerChannel(entry.id, c.id),
            });
        }
    }
    return rows;
}

/** Shows or hides the Capabilities stack. It renders while the stack exists
 *  (always, once the channel has loaded) and is teal while it is shown. */
function CapabilitiesPill({ stack, shown, onToggle }: {
    stack: Stack | null; shown: boolean; onToggle: () => void;
}) {
    if (!stack) return null;
    const title = shown
        ? 'Capabilities: open on the canvas. Click to hide.'
        : 'Capabilities: hidden. Click to show it as a stack.';
    return (
        <button
            type="button"
            aria-pressed={shown}
            title={title}
            onClick={onToggle}
            className={cn(pill, shown ? 'text-teal' : 'text-ink-2', 'hover:bg-sheet-2 hover:text-ink')}
        >
            Capabilities
        </button>
    );
}

/** The px the centre column may take: the grid's `1fr auto 1fr` keeps the
 *  scrubber centred, so it gets what is left after twice the wider side. */
function useCentreBudget() {
    const barRef = useRef<HTMLDivElement>(null);
    const leftRef = useRef<HTMLDivElement>(null);
    const rightRef = useRef<HTMLDivElement>(null);
    const [budget, setBudget] = useState(Infinity);
    useLayoutEffect(() => {
        const bar = barRef.current;
        const left = leftRef.current;
        const right = rightRef.current;
        if (!bar || !left || !right) return;
        const measure = () => {
            const style = getComputedStyle(bar);
            const inner = bar.clientWidth - parseFloat(style.paddingLeft || '0') - parseFloat(style.paddingRight || '0');
            // Not laid out (a hidden tab, a test): no window.
            const next = bar.clientWidth === 0 ? Infinity : Math.max(0, inner - 2 * Math.max(left.offsetWidth, right.offsetWidth));
            setBudget(prev => (prev === next ? prev : next));
        };
        measure();
        const ro = new ResizeObserver(measure);
        for (const el of [bar, left, right]) ro.observe(el);
        return () => ro.disconnect();
    }, []);
    return { barRef, leftRef, rightRef, budget };
}

export function AppTopBar({
    activeChannel, channels,
    columns, selectedColumnId, visibleTopIndices, fanName, onSelectColumn,
    isZoomedOut, toggleZoom,
    capabilitiesStack, capabilitiesShown, onToggleCapabilities,
    onNewStack, commandsOpen, onCommands,
    onSelectChannel, onSelectServerChannel, onRequestNewChannel, onRenameChannel, onDeleteChannel, onOpenSettings,
}: AppTopBarProps) {
    const { barRef, leftRef, rightRef, budget } = useCentreBudget();
    const chanBtnRef = useRef<HTMLButtonElement>(null);
    const [menuOpen, setMenuOpen] = useState(false);
    const [channelDialog, setChannelDialog] = useState<null | 'rename' | 'delete'>(null);
    const [dialogInput, setDialogInput] = useState('');
    // The shell lists again (and polls at once) each time the menu opens.
    const catalog = useServerCatalog(menuOpen);
    const multiServer = desktopBridge()?.servers !== undefined;

    const newStackTooltip = useShortcutTooltip(SHORTCUT_IDS.STACK_NEW_CELL, 'New stack');
    const toggleKeys = useShortcutDisplay(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    const openKeys = useShortcutDisplay(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS);
    const commandsTitle = ['Commands', [toggleKeys && `${toggleKeys} toggles`, openKeys && `${openKeys} opens`].filter(Boolean).join(', ')]
        .filter(Boolean).join(' · ');
    const zoomLabel = isZoomedOut ? 'Zoom back in' : 'Zoom out to the overview';

    const openChannelDialog = (mode: 'rename' | 'delete') => {
        setDialogInput(mode === 'rename' ? activeChannel.name : '');
        setChannelDialog(mode);
    };
    const closeChannelDialog = () => {
        setChannelDialog(null);
        setDialogInput('');
    };
    const submitChannelDialog = () => {
        if (channelDialog === 'rename') {
            const name = dialogInput.trim();
            if (name && name !== activeChannel.name) onRenameChannel(name);
        } else if (channelDialog === 'delete') {
            if (dialogInput.trim() !== activeChannel.name) return;
            onDeleteChannel();
        }
        closeChannelDialog();
    };

    const channelItems: MenuEntry[] = [
        ...channelRows({
            channels, activeChannel, catalog: multiServer ? catalog : null,
            onSelectChannel, onSelectServerChannel,
        }),
        { kind: 'separator', id: 'operations' },
        { kind: 'item', id: 'new', label: 'New channel…', icon: Plus, onSelect: onRequestNewChannel },
        { kind: 'item', id: 'rename', label: 'Rename current…', icon: Pencil, onSelect: () => openChannelDialog('rename') },
        { kind: 'item', id: 'delete', label: 'Delete current…', icon: Trash2, danger: true, onSelect: () => openChannelDialog('delete') },
        { kind: 'separator', id: 'app' },
        { kind: 'item', id: 'settings', label: 'Settings…', icon: Settings, shortcut: SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS, onSelect: onOpenSettings },
    ];

    return (
        <div
            ref={barRef}
            data-top-bar
            className="grid h-9 flex-none grid-cols-[1fr_auto_1fr] items-center px-2.5 border-b border-line bg-canvas text-[0.78125rem] text-ink-2"
        >
            <div ref={leftRef} className="flex items-center gap-0.5 justify-self-start min-w-0">
                {/* In the desktop shell the mark toggles the title bar row
                    above (shown: hide it; hidden: restore it); inert in a
                    browser tab, which has no such row. */}
                <TopBarMark />
                <button
                    ref={chanBtnRef}
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    title="Channels"
                    onClick={() => setMenuOpen(o => !o)}
                    className={cn(tbBtn, tbBtnOn, 'min-w-0')}
                >
                    <Hash aria-hidden className="size-3.5 flex-none -mr-0.5 text-ink-3" strokeWidth={1.6} />
                    <span className="truncate font-medium text-ink">{activeChannel.name}</span>
                    <ChevronDown aria-hidden className="size-3.5 flex-none" strokeWidth={1.6} />
                </button>
                <Menu
                    anchor={chanBtnRef}
                    open={menuOpen}
                    onClose={() => setMenuOpen(false)}
                    role="menu"
                    label="Channels"
                    items={channelItems}
                    align="start"
                    minWidth={248}
                />
                <Sep />
                <button
                    type="button"
                    aria-label="New stack"
                    title={newStackTooltip}
                    onClick={onNewStack}
                    className={tbBtn}
                >
                    <Plus className="size-3.5" strokeWidth={1.6} />
                </button>
                <button
                    type="button"
                    aria-label="Commands"
                    title={commandsTitle}
                    aria-pressed={commandsOpen}
                    onClick={onCommands}
                    {...{ [COMMANDS_TOGGLE_ATTR]: '' }}
                    className={cn(tbBtn, commandsOpen && 'text-teal')}
                >
                    <Search className="size-3.5" strokeWidth={1.6} />
                </button>
            </div>

            <ChannelScrubber
                columns={columns}
                selectedId={selectedColumnId}
                visibleTopIndices={visibleTopIndices}
                fanName={fanName}
                onSelect={onSelectColumn}
                maxWidth={budget}
            />

            <div ref={rightRef} className="flex items-center gap-0.5 justify-self-end">
                <RunStatusIndicator
                    activeChannel={activeChannel}
                    isZoomedOut={isZoomedOut}
                    onExitZoom={() => { if (isZoomedOut) toggleZoom(); }}
                    onSelectChannel={onSelectChannel}
                />
                <Sep />
                <CapabilitiesPill stack={capabilitiesStack} shown={capabilitiesShown} onToggle={onToggleCapabilities} />
                <button
                    type="button"
                    aria-pressed={isZoomedOut}
                    aria-label={zoomLabel}
                    title={zoomLabel}
                    onClick={toggleZoom}
                    className={cn(tbBtn, tbBtnOn)}
                >
                    {isZoomedOut
                        ? <Shrink className="size-3.5" strokeWidth={1.6} />
                        : <LayoutGrid className="size-3.5" strokeWidth={1.6} />}
                </button>
            </div>

            {channelDialog && (
                <ChannelDialog
                    mode={channelDialog}
                    activeChannelName={activeChannel.name}
                    value={dialogInput}
                    onChange={setDialogInput}
                    onCancel={closeChannelDialog}
                    onSubmit={submitChannelDialog}
                />
            )}
        </div>
    );
}
