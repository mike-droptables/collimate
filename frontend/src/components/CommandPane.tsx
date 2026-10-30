// The Commands pane (the mock's `.cmdk`): a 352px control docked flat
// against the canvas's left or right edge, over the canvas. It acts on the
// selected cell, which stays selected in the canvas: the target row names
// it, Ctrl-←/→ moves it without leaving the pane (the scene keyboard reads
// those keys in the pane), and the camera keeps it clear of the pane
// (useStackCamera's inset; closing the pane slides the canvas back to where
// it would rest without it). No brass here: brass stays on the cell's edge.
//
// It has views (`data-pane-view`): the commands (the query and the list),
// the settings (SettingsView: the sections and their bodies) and a picker
// (the selected cell's Files or runner panel, which CellEditor portals into
// the pane's panel slot while the pane is open; paneHostsPanel). Esc steps
// back one level: a settings section → the settings root → the commands →
// the pane closes and the keyboard goes to the selected cell.
//
// While open the pane holds the keyboard: it is not a keyboard owner (the
// scene's shortcuts and the command bindings keep firing from it), but
// focus never leaves it except through a pointer press (which also closes
// it, in the capture phase, so the press still lands), a modal, or a
// keyboard owner a shortcut opened (a title editor, a menu); Tab wraps
// inside it. Shift+Enter on a command row records a binding for it.
//
// Over the full-window view (CellEditor's maximised content region, which
// covers the top bar and the canvas) the same pane is portaled above it,
// running the window's height on its side, and the view's region insets
// by its width. Its commands act as in the canvas: those that stay on the
// shown cell keep the view; any other (another cell, the stack, the
// channel) first closes the view, then acts. With no channel open (the
// channel-creation view) the pane has no scene: it is portaled the same
// way and offers its dock row and the Settings rows.
//
// The host stays mounted while the pane is closed: it builds the rows from
// the scene on every render, so the command bindings (useCommandBindings)
// fire on the current selection with the pane open or closed.

import { createElement, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useShallow } from 'zustand/react/shallow';
import { PanelLeft, PanelRight, Search, type LucideProps } from 'lucide-react';
import { cn } from '@/lib/utils';
import { kbd } from '@/components/ui/formStyles';
import { isCapabilitiesStack, type Cell, type Stack } from '@/lib/types';
import type { CellLayout, FilesPaneSide, RunsPaneSide } from '@/stores/collimate/types';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { FanRange } from '@/lib/fanOut';
import { frontCellOf, isManageFront } from '@/lib/frontSlots';
import {
    activeTab, activateTab, allTabs, closeHistoryTabs, dockFilesPane, dockRunsPane, filesPaneCollapsed, setFilesPane,
} from '@/lib/cellLayout';
import { STATUS_VIEW } from '@/stores/collimate/annotationState';
import { hasOptionsStack } from '@/lib/fileTree';
import { dotStatus, runsOnCell, type DotStatus } from '@/lib/runningCell';
import { isFailureStatus } from '@/lib/runStop';
import { resolveIcon, DEFAULT_GROUP_ICON } from '@/lib/iconRegistry';
import { useGuestOccluder } from '@/lib/guestOcclusion';

import { desktopServers } from '@/lib/desktop';
import { claimKeyboardForGesture, KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';
import { wrapTab } from '@/lib/modalKeys';
import { openFilesPanel } from '@/hooks/sceneKeyboardEffects';
import { is } from '@/lib/keybindings/handlers';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { SHORTCUT_REGISTRY } from '@/lib/keybindings/registry';
import { formatCombo, parseCombo } from '@/lib/keybindings/parser';
import { bindingPermitted, comboFromKeydown } from '@/lib/keybindings/record';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import { displayComboString } from '@/lib/keybindings/display';
import { useCommandBindings } from '@/lib/keybindings/commandBindings';
import type { KeyCombo } from '@/lib/keybindings/types';
import { COMMAND_PANE_ATTR, COMMANDS_TOGGLE_ATTR, PANE_ENTRY_ATTR, PANE_PANEL_SLOT_ATTR, PANE_QUERY_ATTR, paneHostsPanel } from '@/lib/commandPane';
import { focusPaneTarget, fullWindowRoot, leaveFullWindow } from '@/lib/fullWindowView';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH, type SettingsSection } from '@/stores/collimate/commandPaneSlice';
import { SettingsView } from './commandPane/SettingsView';
import { PaneFooter } from './commandPane/PaneFooter';
import { PANE_GROUP_HEADING } from './commandPane/paneStyles';
import { settingsSectionOf } from './commandPane/settingsSections';
import {
    bindingConflicts, bindingOf, buildPaneCommands, matchParts, visiblePaneCommands,
    type CommandHandlers, type CommandSnapshot, type PaneCommand,
} from './commandPaneCommands';

/** The Capabilities stack's channel rows as the pane offers them. */
export interface CapabilitiesStackCommands {
    /** Capabilities is on the canvas (else hidden to the top bar). */
    shown: boolean;
    /** Hide it, or show it on its manage card. */
    toggle: () => void;
    /** Open it on its Create questionnaire. */
    create: () => void;
}

/** The scene the pane acts on: the canvas columns (a fanned stack as its
 *  columns), the selection and the scene's actions. */
export interface CommandPaneScene {
    displayStacks: Stack[];
    displaySelectedId: string | null;
    visibleTopIndices: Record<string, number>;
    fanRange: FanRange | null;
    selectDisplayId: (id: string) => void;
    setFannedOutStackId: (id: string | null) => void;
    actions: SceneActions;
    mainStacks: Stack[];
    /** The selected (real) stack. */
    selectedStackId: string | null;
    isZoomedOut: boolean;
    toggleZoom: () => void;
    capabilities: CapabilitiesStackCommands;
}

export interface CommandPaneProps {
    /** null: no channel is open; the pane offers its dock row and Settings,
     *  over the channel-creation view. */
    scene: CommandPaneScene | null;
}

// The light's grammar (the mock's `.light`): a ring at rest, teal while a run
// is live, brass while one waits on the user, a red ring for an unseen failure.
const LIGHT: Record<DotStatus | 'failed' | 'idle', string> = {
    running: 'border-teal bg-teal shadow-[0_0_0_3px_var(--teal-soft)]',
    spinning_up: 'border-teal bg-teal-soft',
    awaiting_approval: 'border-brass bg-brass shadow-[0_0_0_3px_var(--brass-soft)]',
    awaiting_input: 'border-brass bg-brass shadow-[0_0_0_3px_var(--brass-soft)]',
    stopping: 'border-red bg-red-soft',
    failed: 'border-red bg-transparent',
    idle: 'border-ink-3 bg-transparent',
};

/** The cell's file tabs other than the open one (the mock's `.tab:not(.on)`),
 *  one per file (a file open in two groups is one row). */
function otherFileTabs(layout: CellLayout): { id: string; path: string; name: string }[] {
    const open = activeTab(layout);
    const byPath = new Map<string, { id: string; path: string; name: string }>();
    for (const t of allTabs(layout)) {
        if (t.kind !== 'file' || t.id === open?.id || byPath.has(t.path)) continue;
        byPath.set(t.path, { id: t.id, path: t.path, name: t.path.split('/').pop() ?? t.path });
    }
    return [...byPath.values()];
}

const overlaps = (a: DOMRect, b: DOMRect) =>
    a.width > 0 && a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom;

/** The cells whose guests the pane covers, re-measured while it is open (the
 *  canvas pans and the cells move under it). Over the full-window view the
 *  cards lie under the view, whose own occluder freezes them all but the
 *  shown cell's; that one's guests freeze only while its region still
 *  reaches under the pane (the region's slide). */
function useCellsUnder(paneRef: React.RefObject<HTMLElement | null>, fullWindowCellId: string | null): string[] {
    const [ids, setIds] = useState<string[]>([]);
    useEffect(() => {
        let raf = 0;
        let last = -Infinity;
        let key = '';
        const measure = (now: number) => {
            raf = requestAnimationFrame(measure);
            if (now - last < 120) return;
            last = now;
            const pane = paneRef.current?.getBoundingClientRect();
            if (!pane) return;
            const under = new Set<string>();
            if (fullWindowCellId !== null) {
                const region = fullWindowRoot()?.querySelector('[data-content-region]');
                if (region && overlaps(region.getBoundingClientRect(), pane)) under.add(fullWindowCellId);
            } else {
                for (const el of document.querySelectorAll<HTMLElement>('[data-cell-id]')) {
                    if (overlaps(el.getBoundingClientRect(), pane)) under.add(el.dataset.cellId!);
                }
            }
            const next = [...under].sort();
            const nextKey = next.join(',');
            if (nextKey !== key) {
                key = nextKey;
                setIds(next);
            }
        };
        raf = requestAnimationFrame(measure);
        return () => cancelAnimationFrame(raf);
    }, [paneRef, fullWindowCellId]);
    return ids;
}

/** What the pane acts on and the rows it offers, built from the scene. */
interface PaneModel {
    cell: Cell | null;
    cellId: string | null;
    stack: Stack | null;
    /** The target cell sits on Capabilities. */
    capabilitiesCell: boolean;
    fanned: boolean;
    handlers: CommandHandlers;
    all: PaneCommand[];
    /** Inside the desktop shell (the desktop-only settings sections). */
    desktop: boolean;
    /** The overview: no cell editor is mounted, so none hosts a picker. */
    zoomedOut: boolean;
}

/** The pane's host, mounted with the scene (or with none, over the
 *  channel-creation view): it keeps the command bindings live while there
 *  is a scene and mounts the pane while it is open. */
export function CommandPane({ scene }: CommandPaneProps) {
    const open = useCollimateStore(s => s.commandPane.open);
    const model = usePaneModel(scene);
    return (
        <>
            {scene && <CommandBindings commands={model.all} />}
            {open && <OpenCommandPane model={model} sceneless={scene === null} />}
        </>
    );
}

/** The command bindings over the latest rows. */
function CommandBindings({ commands }: { commands: PaneCommand[] }) {
    const latest = useRef(commands);
    useLayoutEffect(() => { latest.current = commands; });
    useCommandBindings(useCallback(() => latest.current, []));
    return null;
}

const noop = () => {};

/** The scene of a pane with no channel: nothing to act on. */
const NO_SCENE: CommandPaneScene = {
    displayStacks: [], displaySelectedId: null, visibleTopIndices: {}, fanRange: null,
    selectDisplayId: noop, setFannedOutStackId: noop, actions: {} as SceneActions,
    mainStacks: [], selectedStackId: null, isZoomedOut: false, toggleZoom: noop,
    capabilities: { shown: false, toggle: noop, create: noop },
};

/** The target (the selected column's front cell and its real stack), the
 *  scene snapshot, the handlers and the rows they make. */
function usePaneModel(scene: CommandPaneScene | null): PaneModel {
    const {
        displayStacks, displaySelectedId, visibleTopIndices, fanRange, selectDisplayId, setFannedOutStackId,
        actions, mainStacks, selectedStackId, isZoomedOut, toggleZoom, capabilities: capabilitiesStackCommands,
    } = scene ?? NO_SCENE;
    const side = useCollimateStore(s => s.commandPane.side);
    const desktop = desktopServers() !== undefined;

    const column = displayStacks.find(s => s.id === displaySelectedId) ?? null;
    // The column's front cell: none while Capabilities fronts its manage
    // card, so the pane offers no cell rows there.
    const cell = frontCellOf(column, visibleTopIndices);
    const stack = mainStacks.find(s => s.id === selectedStackId) ?? null;
    const cellId = cell ? String(cell.id) : null;

    const layout = useCollimateStore(s => (cellId ? s.cellLayouts[cellId] : undefined));
    const liveRuns = useCollimateStore(s => (cellId ? runsOnCell(s, cellId).length : 0));
    const channelId = useCollimateStore(s => s.activeChannel?.id ?? '');
    const running = useCollimateStore(useShallow(s => {
        const here = channelId;
        let mine = 0;
        let others = 0;
        for (const entry of s.activeRunsEverywhere) {
            const n = entry.runs.filter(run => !run.stopping && !s.stoppingRunIds.has(run.run_id)).length;
            if (entry.channel === here) mine += n;
            else others += n;
        }
        return { mine, others };
    }));

    const capabilitiesCell = isCapabilitiesStack(stack);
    const fanned = !!stack && fanRange?.stackId === stack.id;
    const frontIdx = stack && cell ? Math.max(0, stack.cells.findIndex(c => String(c.id) === cellId)) : 0;

    const snapshot: CommandSnapshot = {
        zoomedOut: isZoomedOut,
        side,
        desktop,
        target: stack && cell ? {
            cellId: String(cell.id),
            stackId: stack.id,
            stackName: stack.name,
            capabilities: capabilitiesCell,
            builtin: !!cell.builtin,
            editor: isZoomedOut ? null : {
                statusPane: layout ? { open: layout.runsPane.open, side: layout.runsPane.side } : null,
                filesShown: layout ? !filesPaneCollapsed(layout) : null,
                filesSide: layout ? layout.filesPane.side : null,
                otherTabs: layout ? otherFileTabs(layout) : [],
            },
            liveRuns,
            canSplit: !capabilitiesCell && liveRuns === 0 && hasOptionsStack(cell),
            cellCount: stack.cells.length,
            frontIdx,
            fanned,
        } : null,
        channel: scene ? {
            capabilitiesShown: capabilitiesStackCommands.shown,
            runningHere: running.mine,
            runningElsewhere: running.others,
        } : null,
        visibleCells: displayStacks.flatMap((col): CommandSnapshot['visibleCells'] => {
            if (col.id === displaySelectedId) return [];
            if (isManageFront(col, visibleTopIndices)) {
                return [{ columnId: col.id, cellId: null, title: 'Capabilities', stackName: '' }];
            }
            const front = frontCellOf(col, visibleTopIndices);
            if (!front) return [];
            const owner = col.synthetic ? mainStacks.find(s => s.id === col.sourceStackId) : col;
            return [{ columnId: col.id, cellId: String(front.id), title: front.name, stackName: owner?.name ?? '' }];
        }),
    };

    const store = useCollimateStore.getState;
    // A command on the cell the full-window view shows acts inside the view
    // (its panel, status pane, tabs and files are in the view's region); one that
    // reaches past it closes the view first. Read when it runs.
    const inView = <A extends unknown[]>(run: (...args: A) => void) => (...args: A) => {
        if (store().maximizedOverlayCellId !== cellId) leaveFullWindow();
        run(...args);
    };
    const past = <A extends unknown[]>(run: (...args: A) => void) => (...args: A) => {
        leaveFullWindow();
        run(...args);
    };
    // A surface outside the pane that takes the keyboard: the open pane
    // holds the keyboard, so it closes first.
    const outside = <A extends unknown[]>(run: (...args: A) => void) => (...args: A) => {
        store().closeCommandPane();
        run(...args);
    };
    const handlers: CommandHandlers = {
        openRunners: inView(() => { if (displaySelectedId) store().requestOpenGeneratorRunner(displaySelectedId); }),
        findFile: inView(() => { if (cellId) openFilesPanel(cellId); }),
        // The cell's transfer panel (Import / export). Not on a cell in a
        // cell diff (no panel shows there); version history is left first.
        openTransfer: inView(outside(() => {
            if (!cellId || store().cellDiffs[cellId]) return;
            if (store().cellHistory[cellId]) {
                store().closeCellHistory(cellId);
                store().updateCellLayout(cellId, l => closeHistoryTabs(l));
            }
            store().openPickSession(cellId, 'transfer');
        })),
        // The status pane is in the cell's region: in the view while the
        // full-window view shows the cell.
        showLog: inView(outside(() => { if (cellId) store().openRunsPaneOn(cellId, { view: 'all', option: 'log' }); })),
        openStatus: inView(outside(() => { if (cellId) store().openRunsPaneOn(cellId, STATUS_VIEW); })),
        annotate: inView(outside(() => {
            if (cellId) store().openRunsPaneOn(cellId, { view: 'all', option: 'default' }, { focus: 'composer' });
        })),
        openTab: inView((tabId: string) => { if (cellId) store().updateCellLayout(cellId, l => activateTab(l, tabId)); }),
        setFilesShown: inView((shown: boolean) => { if (cellId) store().updateCellLayout(cellId, l => setFilesPane(l, { collapsed: !shown })); }),
        dockFiles: inView((side: FilesPaneSide) => { if (cellId) store().updateCellLayout(cellId, l => dockFilesPane(l, side)); }),
        dockStatus: inView((side: RunsPaneSide) => { if (cellId) store().updateRunsPane(cellId, l => dockRunsPane(l, side)); }),
        stepCell: past((dir: -1 | 1) => { if (stack) actions.winRotate(stack.id, dir); }),
        setFanned: past((next: boolean) => setFannedOutStackId(next && stack ? stack.id : null)),
        split: past(() => { if (cellId && stack) void actions.splitCell(cellId, stack.id); }),
        toggleCapabilitiesStack: past(capabilitiesStackCommands.toggle),
        // The questionnaire takes the keyboard on the manage card.
        createCapability: past(outside(capabilitiesStackCommands.create)),
        openRunsMenu: past(outside(() => store().setRunsMenuOpen(true))),
        toggleZoom: past(toggleZoom),
        dock: (next) => store().setCommandPaneSide(next),
        newStack: past(() => { void actions.newEmptyStackRight(selectedStackId); }),
        // The settings live in the pane, over the full-window view as over
        // the canvas.
        openSettings: (section) => store().openCommandPaneSettings(section),
        goTo: past((columnId: string) => selectDisplayId(columnId)),
        resetShortcuts: () => {
            void store().openConfirm({
                title: 'Reset shortcuts',
                body: 'Reset every shortcut to its default and clear every command binding?',
                confirmLabel: 'Reset',
                destructive: true,
            }).then(ok => { if (ok) useKeybindingsStore.getState().resetAll(); });
        },
    };

    return { cell, cellId, stack, capabilitiesCell, fanned, handlers, desktop, zoomedOut: isZoomedOut, all: buildPaneCommands(snapshot, handlers) };
}

/** Record a row's binding: a registry-backed row overrides its shortcut
 *  (the registry's own combo clears the override), any other row binds its
 *  command key; null unbinds. */
function bindRow(cmd: PaneCommand, combo: KeyCombo | null): void {
    const keys = useKeybindingsStore.getState();
    if (!cmd.shortcut) {
        keys.setCommandBinding(cmd.key, combo);
        return;
    }
    const fallback = parseCombo(SHORTCUT_REGISTRY[cmd.shortcut]?.default ?? '');
    const isDefault = !!combo && !!fallback && formatCombo(combo) === formatCombo(fallback);
    keys.setOverride(cmd.shortcut, combo && !isDefault ? combo : null);
}

/** A modal over the pane: it keeps the keyboard and a press in it leaves
 *  the pane open. The channel-creation view is aria-modal but lies under
 *  the pane. */
const MODAL_OVER_PANE = '[aria-modal="true"]:not([data-channel-create])';

/** What a press in the pane may focus. */
const PANE_FOCUSABLE = 'input, textarea, select, button, a[href], label, [tabindex], [contenteditable]';

/** The press is Shift+Enter alone: the binding capture's key. */
const isBindKey = (e: KeyboardEvent) => e.key === 'Enter' && e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey;

/** The commands view's footer hints. */
const COMMANDS_HINTS = [['↑↓', 'move'], ['↵', 'run'], ['⇧↵', 'bind']] as const;

function OpenCommandPane({ model, sceneless }: { model: PaneModel; sceneless: boolean }) {
    const { cell, cellId, stack, capabilitiesCell, fanned, handlers, all, desktop, zoomedOut } = model;
    const { side, query, recents, view } = useCollimateStore(useShallow(s => s.commandPane));
    // The cell shown in the full-window view, while it is open.
    const fullWindowCellId = useCollimateStore(s => s.maximizedOverlayCellId);
    // The selected cell's picker, rendered into the panel slot by its
    // editor (none in the overview).
    const hosted = useCollimateStore(s => !zoomedOut && s.selectedCellId !== null && paneHostsPanel(s, s.selectedCellId));
    const paneRef = useRef<HTMLElement>(null);
    const inputRef = useRef<HTMLInputElement>(null);
    const listRef = useRef<HTMLDivElement>(null);
    const listId = useId();
    // The section the settings root came back from (its row takes the keyboard).
    const [fromSection, setFromSection] = useState<SettingsSection | null>(null);
    const light = useCollimateStore((s): keyof typeof LIGHT => {
        if (!cellId) return 'idle';
        const last = s.lastFinishedByCell[cellId];
        return dotStatus(s, cellId) ?? (last && !last.seen && isFailureStatus(last.status) ? 'failed' : 'idle');
    });
    // The kbd hints, the combo search and the conflicts read the bindings.
    const keys = useKeybindingsStore(useShallow(s => ({ overrides: s.overrides, commands: s.commands, resolveString: s.resolveString })));
    const capturing = useKeybindingsStore(s => s.capturing);
    const store = useCollimateStore.getState;

    const settingsSection = view.kind === 'settings' && view.section && settingsSectionOf(view.section, desktop) ? view.section : null;
    // The settings view shows over a hosted picker (which waits in its
    // cell meanwhile: paneHostsPanel reads the view).
    const paneView = view.kind === 'settings' ? 'settings' : hosted ? 'picker' : 'commands';

    // The scene keyboard moves the target from the pane too (Ctrl-←/→): a
    // target that leaves the cell the full-window view shows closes it.
    const prevCellId = useRef(cellId);
    useEffect(() => {
        const from = prevCellId.current;
        prevCellId.current = cellId;
        if (from !== cellId && from !== null && store().maximizedOverlayCellId === from) leaveFullWindow();
    }, [cellId, store]);

    const rows = visiblePaneCommands(all, query, recents, keys);
    const rowId = (i: number) => `${listId}-${i}`;
    // The highlighted row, held with the results it was picked in: a new
    // query or a change in the rows (a recent recorded, another target cell)
    // puts it back on the first row, and the list back at its top.
    const results = `${query}\n${rows.map(c => `${c.group}:${c.key}`).join('|')}`;
    const [picked, setPicked] = useState({ results, index: 0 });
    const activeIdx = rows.length === 0 ? -1 : picked.results === results ? picked.index : 0;
    const setActive = (index: number) => setPicked({ results, index });
    useLayoutEffect(() => {
        if (listRef.current) listRef.current.scrollTop = 0;
    }, [results]);
    // The pointer's last position over the list. Only a pointer that moved
    // highlights a row: rows changing under a still pointer (typing, a
    // command run) get a mousemove of their own in Chromium, at the same spot.
    const pointer = useRef<{ x: number; y: number } | null>(null);
    const onListMouseMove = (e: React.MouseEvent) => {
        const last = pointer.current;
        pointer.current = { x: e.clientX, y: e.clientY };
        if (!last || (last.x === e.clientX && last.y === e.clientY)) return;
        const row = (e.target as Element).closest<HTMLElement>('[role="option"]');
        const index = row ? Number(row.dataset.row) : -1;
        if (index >= 0 && index !== activeIdx) setActive(index);
    };

    const occluded = useCellsUnder(paneRef, fullWindowCellId);
    useGuestOccluder({ cellIds: occluded }, occluded.length > 0);

    // The view's entry field: the hosted panel's search, else the element
    // the view marks (the query, a section's back button, the settings
    // root's row).
    const focusEntry = useCallback(() => {
        const root = paneRef.current;
        if (!root) return;
        const slot = root.querySelector(`[${PANE_PANEL_SLOT_ATTR}]`);
        const target = slot?.querySelector<HTMLElement>('input') ?? root.querySelector<HTMLElement>(`[${PANE_ENTRY_ATTR}]`);
        target?.focus({ preventScroll: true });
    }, []);

    // Opening, docking and every change of view put the keyboard on the
    // view's entry field.
    useLayoutEffect(() => {
        focusEntry();
    }, [side, paneView, settingsSection, focusEntry]);
    // Moving onto or off the full-window view (or over the channel-creation
    // view) mounts the pane's element anew; the keyboard it held comes back.
    const overlay = sceneless || fullWindowCellId !== null;
    const placed = useRef(overlay);
    useLayoutEffect(() => {
        if (placed.current === overlay) return;
        placed.current = overlay;
        if (!document.activeElement || document.activeElement === document.body) focusEntry();
    }, [overlay, focusEntry]);

    // The panel slot: registered while the pane is mounted, so the selected
    // cell's picker renders here (paneHostsPanel); cleared on unmount.
    const slotRef = useCallback((el: HTMLDivElement | null) => {
        useCollimateStore.getState().setCommandPanePanelSlot(el);
    }, []);

    // The keyboard stays in the pane. A press anywhere off it closes it, in
    // the capture phase so the press still lands where it was aimed (the
    // keyboard goes where it lands): a press into a renderer frame, which
    // the page never sees, closes it as its focus arrives. The Commands
    // button toggles the pane itself, and a modal is over it. Focus that
    // moves off it any other way (a programmatic focus, a frame taking it
    // with no pointer over it, a focused element going away) comes back to
    // the entry field, unless a modal or a keyboard owner took it: a
    // surface a shortcut opened from the pane (a title editor, a menu)
    // keeps the keyboard until it lets go.
    useEffect(() => {
        const isOpen = () => useCollimateStore.getState().commandPane.open;
        const inModal = (node: EventTarget | null) => node instanceof Element && node.closest(MODAL_OVER_PANE) !== null;
        const keepsFocus = (node: EventTarget | null) =>
            node instanceof Element && (inModal(node) || node.closest(`[${KEYBOARD_OWNER_ATTR}]`) !== null);
        const lostToBody = () => {
            const active = document.activeElement;
            return (!active || active === document.body) && !document.querySelector(MODAL_OVER_PANE);
        };
        // The frame under the pointer: its focus arriving is a press.
        let frameUnderPointer: Element | null = null;
        let refocus = 0;
        const onPointerOver = (e: PointerEvent) => {
            frameUnderPointer = e.target instanceof HTMLIFrameElement ? e.target : null;
        };
        const onPointerDown = (e: PointerEvent) => {
            const target = e.target as Node | null;
            if (!target || paneRef.current?.contains(target)) return;
            if (target instanceof Element && target.closest(`[${COMMANDS_TOGGLE_ATTR}]`)) return;
            if (inModal(target)) return;
            useCollimateStore.getState().closeCommandPane();
        };
        const onFocusIn = (e: FocusEvent) => {
            if (!isOpen()) return;
            const target = e.target as Node | null;
            if (!target || paneRef.current?.contains(target) || keepsFocus(target)) return;
            focusEntry();
        };
        const onFocusOut = (e: FocusEvent) => {
            if (e.relatedTarget !== null) return;
            cancelAnimationFrame(refocus);
            refocus = requestAnimationFrame(() => {
                if (isOpen() && lostToBody()) focusEntry();
            });
        };
        // A key with nowhere to go (focus already on the body) puts the
        // keyboard back in the pane before it is typed.
        const onKeyDown = () => {
            if (isOpen() && lostToBody()) focusEntry();
        };
        const onBlur = () => {
            if (!isOpen()) return;
            const active = document.activeElement;
            if (active?.tagName !== 'IFRAME') return;
            if (active === frameUnderPointer) useCollimateStore.getState().closeCommandPane();
            else focusEntry();
        };
        document.addEventListener('pointerover', onPointerOver, true);
        document.addEventListener('pointerdown', onPointerDown, true);
        window.addEventListener('focusin', onFocusIn, true);
        window.addEventListener('focusout', onFocusOut, true);
        window.addEventListener('keydown', onKeyDown, true);
        window.addEventListener('blur', onBlur);
        return () => {
            cancelAnimationFrame(refocus);
            document.removeEventListener('pointerover', onPointerOver, true);
            document.removeEventListener('pointerdown', onPointerDown, true);
            window.removeEventListener('focusin', onFocusIn, true);
            window.removeEventListener('focusout', onFocusOut, true);
            window.removeEventListener('keydown', onKeyDown, true);
            window.removeEventListener('blur', onBlur);
        };
    }, [focusEntry]);

    const activeRowId = activeIdx >= 0 ? rowId(activeIdx) : undefined;
    useEffect(() => {
        if (activeRowId) document.getElementById(activeRowId)?.scrollIntoView?.({ block: 'nearest' });
    }, [activeRowId]);

    // The binding capture (Shift+Enter on a row): the next chord binds the
    // row. It holds the keyboard from the scene and the bindings while it
    // listens; Esc cancels, a bare Backspace / Delete clears the binding, a
    // bare modifier waits for the rest of the chord, and a chord the row's
    // shortcut may not take (a cell move without a modifier) waits for
    // another. A press anywhere cancels.
    const latestAll = useRef(all);
    useLayoutEffect(() => { latestAll.current = all; });
    useEffect(() => {
        if (capturing === null) return;
        const release = claimKeyboardForGesture();
        const stop = () => useKeybindingsStore.getState().setCapturing(null);
        const onKeyDown = (e: KeyboardEvent) => {
            e.preventDefault();
            e.stopPropagation();
            const cmd = latestAll.current.find(c => c.key === capturing);
            if (!cmd || e.key === 'Escape') { stop(); return; }
            const bare = !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey;
            if (bare && (e.key === 'Backspace' || e.key === 'Delete')) {
                bindRow(cmd, null);
                stop();
                return;
            }
            const combo = comboFromKeydown(e);
            if (!combo || (cmd.shortcut && !bindingPermitted(cmd.shortcut, combo))) return;
            bindRow(cmd, combo);
            stop();
        };
        window.addEventListener('keydown', onKeyDown, true);
        window.addEventListener('pointerdown', stop, true);
        return () => {
            window.removeEventListener('keydown', onKeyDown, true);
            window.removeEventListener('pointerdown', stop, true);
            release();
        };
    }, [capturing]);
    // The capture ends with its row: the row leaving the list, the view
    // changing, the pane closing.
    const capturedShown = capturing !== null && paneView === 'commands' && rows.some(c => c.key === capturing);
    useEffect(() => {
        if (capturing !== null && !capturedShown) useKeybindingsStore.getState().setCapturing(null);
    }, [capturing, capturedShown]);
    useEffect(() => () => useKeybindingsStore.getState().setCapturing(null), []);

    const closeToCell = () => {
        store().closeCommandPane();
        focusPaneTarget(cellId);
    };

    /** Esc's step back: a section → the settings root → the commands →
     *  closed, the keyboard on the selected cell. */
    const back = () => {
        if (view.kind === 'commands') {
            closeToCell();
        } else if (settingsSection) {
            setFromSection(settingsSection);
            store().setCommandPaneView({ kind: 'settings', section: null });
        } else {
            store().setCommandPaneView({ kind: 'commands' });
        }
    };

    const exec = (cmd: PaneCommand | undefined) => {
        if (!cmd) return;
        cmd.run();
        store().recordCommandPaneRecent(cmd.key);
        store().setCommandPaneQuery('');
        if (!cmd.leavesPane) inputRef.current?.focus({ preventScroll: true });
    };

    const onQueryKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        const ev = e.nativeEvent;
        if (isBindKey(ev)) {
            e.preventDefault();
            // A command shows once in the list (under Recent or its own
            // group): its one row records the binding.
            const cmd = rows[activeIdx];
            if (cmd) useKeybindingsStore.getState().setCapturing(cmd.key);
            return;
        }
        if (is(ev, SHORTCUT_IDS.LIST_NEXT)) {
            e.preventDefault();
            if (rows.length) setActive((activeIdx + 1) % rows.length);
        } else if (is(ev, SHORTCUT_IDS.LIST_PREV)) {
            e.preventDefault();
            if (rows.length) setActive((activeIdx - 1 + rows.length) % rows.length);
        } else if (is(ev, SHORTCUT_IDS.LIST_RUN_HIGHLIGHTED)) {
            e.preventDefault();
            exec(rows[activeIdx]);
        }
    };

    // The pane's own keys, from anywhere in its DOM: Alt-↑/↓ step the
    // selected stack; Tab wraps; Esc steps back and goes no further than the
    // pane, so the selected cell's file editor (its window-level Esc closes
    // the file's tab) never sees it. A native listener on the root, since a
    // hosted picker is portaled in from its cell's React tree: Alt-↑/↓ and
    // Tab reach the pane from its search too, while its other keys are its
    // own (its Esc closes it).
    const onPaneKeyDown = (e: KeyboardEvent) => {
        if (is(e, SHORTCUT_IDS.STACK_ROTATE_PREV) || is(e, SHORTCUT_IDS.STACK_ROTATE_NEXT)) {
            e.preventDefault();
            if (stack && !fanned) handlers.stepCell(is(e, SHORTCUT_IDS.STACK_ROTATE_PREV) ? -1 : 1);
            return;
        }
        if (e.key === 'Tab') {
            wrapTab(e, paneRef.current);
            return;
        }
        if (e.target instanceof Element && e.target.closest(`[${PANE_PANEL_SLOT_ATTR}]`)) return;
        if (!is(e, SHORTCUT_IDS.LIST_CLOSE)) return;
        e.preventDefault();
        e.stopPropagation();
        back();
    };
    const paneKeys = useRef(onPaneKeyDown);
    useLayoutEffect(() => { paneKeys.current = onPaneKeyDown; });
    useEffect(() => {
        const root = paneRef.current;
        if (!root) return;
        const onKeyDown = (e: KeyboardEvent) => paneKeys.current(e);
        root.addEventListener('keydown', onKeyDown);
        return () => root.removeEventListener('keydown', onKeyDown);
    }, [overlay]);

    const groups: { name: string; items: { cmd: PaneCommand; i: number }[] }[] = [];
    rows.forEach((cmd, i) => {
        const last = groups[groups.length - 1];
        if (last && last.name === cmd.group) last.items.push({ cmd, i });
        else groups.push({ name: cmd.group, items: [{ cmd, i }] });
    });

    const glyph = createElement(resolveIcon(cell?.icon) ?? DEFAULT_GROUP_ICON, {
        'data-testid': 'command-target-glyph', className: 'size-3.5 text-teal', strokeWidth: 1.6,
    } as LucideProps);
    const left = side === 'left';

    const targetRow = (
        <div
            aria-live="polite"
            title="Commands act on this cell"
            className="flex-none h-[1.875rem] flex items-start gap-2 pt-px px-3.5 border-b border-line text-[0.78125rem] text-ink-3"
        >
            {cell && (
                <>
                    <span className="flex-none h-[1.125rem] inline-flex items-center">
                        {capabilitiesCell
                            ? glyph
                            : <i data-testid="command-target-light" data-light={light}
                                className={cn('block w-[0.5625rem] h-[0.5625rem] rounded-full border-[1.5px]', LIGHT[light])} />}
                    </span>
                    <span className="min-w-0 truncate text-ink font-medium leading-[1.125rem]">{cell.name}</span>
                    <span className="ml-auto flex-none leading-[1.125rem]">{stack?.name}</span>
                </>
            )}
        </div>
    );

    const commandsView = (
        <>
            <label className="flex-none h-9 flex items-center gap-2.5 px-3.5 border-b border-line text-ink-3">
                <Search className="size-3.5 flex-none" strokeWidth={1.6} />
                <input
                    ref={inputRef}
                    {...{ [PANE_QUERY_ATTR]: '', [PANE_ENTRY_ATTR]: '' }}
                    type="text"
                    spellCheck={false}
                    autoComplete="off"
                    placeholder="Type a command or a cell"
                    role="combobox"
                    aria-expanded
                    aria-controls={listId}
                    aria-activedescendant={activeRowId}
                    aria-label="Command"
                    value={query}
                    onChange={e => store().setCommandPaneQuery(e.target.value)}
                    onKeyDown={onQueryKeyDown}
                    className="flex-1 min-w-0 h-full bg-transparent border-0 outline-none p-0 text-ink text-[0.84375rem] placeholder:text-ink-3"
                />
            </label>
            {targetRow}
            <div
                ref={listRef}
                id={listId}
                role="listbox"
                aria-label="Commands"
                onMouseMove={onListMouseMove}
                className="flex-1 min-h-0 overflow-y-auto px-1.5 pt-1 pb-2.5"
            >
                {groups.map(group => (
                    <div key={`${group.name}:${group.items[0].i}`} role="presentation">
                        <h4 role="presentation" className={PANE_GROUP_HEADING}>{group.name}</h4>
                        {group.items.map(({ cmd, i }) => {
                            const on = i === activeIdx;
                            const [before, match, after] = matchParts(cmd.label, query);
                            const binding = bindingOf(cmd, keys);
                            const clashes = binding ? bindingConflicts(all, cmd, keys) : [];
                            const recording = capturing === cmd.key;
                            const Icon = cmd.icon;
                            return (
                                <div
                                    key={cmd.key}
                                    id={rowId(i)}
                                    data-row={i}
                                    role="option"
                                    aria-selected={on}
                                    data-command={cmd.key}
                                    data-capturing={recording || undefined}
                                    title={cmd.description}
                                    onMouseDown={e => e.preventDefault()}
                                    onClick={() => exec(cmd)}
                                    className={cn(
                                        'flex items-center gap-2.5 h-[1.875rem] px-2 rounded-md text-[0.8125rem] cursor-pointer',
                                        on ? 'bg-sheet-2 text-ink' : 'text-ink-2',
                                    )}
                                >
                                    <Icon className={cn('size-3.5 flex-none', on ? 'text-ink-2' : 'text-ink-3')} strokeWidth={1.6} />
                                    <span className="flex-1 min-w-0 truncate">
                                        {before}{match && <b className="text-ink font-semibold">{match}</b>}{after}
                                    </span>
                                    {clashes.length > 0
                                        ? <span data-conflict className="min-w-0 truncate text-red text-[0.75rem] whitespace-nowrap">also {clashes.join(', ')}</span>
                                        : cmd.meta && <span className="min-w-0 max-w-[40%] truncate text-ink-3 text-[0.75rem] whitespace-nowrap">{cmd.meta}</span>}
                                    {recording ? (
                                        <kbd data-binding="capturing" className={cn(kbd, 'border-teal bg-teal text-sheet')}>press keys…</kbd>
                                    ) : binding && (
                                        <kbd
                                            data-binding={binding.source}
                                            className={cn(
                                                kbd,
                                                // A binding the person set keeps a soft teal fill; a
                                                // clash with another row's binding reads red.
                                                binding.source !== 'default' && 'border-teal/50 bg-teal-soft text-ink',
                                                clashes.length > 0 && 'border-red',
                                            )}
                                        >
                                            {displayComboString(binding.combo)}
                                        </kbd>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                ))}
                {rows.length === 0 && (
                    <div className="flex items-center h-[1.875rem] px-2 rounded-md text-[0.8125rem] text-ink-3">No command matches</div>
                )}
            </div>
            <PaneFooter hints={COMMANDS_HINTS} escLabel="to cell" onEsc={closeToCell} />
        </>
    );

    const pane = (
        <aside
            ref={paneRef}
            {...{ [COMMAND_PANE_ATTR]: '' }}
            data-pane-view={paneView}
            aria-label="Commands"
            // A press on the pane's own chrome (a heading, the target row,
            // the list's padding) keeps the keyboard where it is.
            onMouseDown={e => { if (!(e.target as Element).closest(PANE_FOCUSABLE)) e.preventDefault(); }}
            // no-drag over the full-window view: the pane's head lies in the
            // desktop title bar's drag band (see FullscreenContentOverlay).
            style={{ width: COMMAND_PANE_WIDTH, ...(overlay && { WebkitAppRegion: 'no-drag' }) } as React.CSSProperties}
            className={cn(
                'top-0 bottom-0 flex flex-col bg-sheet text-ink',
                overlay ? 'fixed z-[455]' : 'absolute z-20',
                left
                    ? 'left-0 border-r border-line-2 shadow-[24px_0_40px_-18px_var(--lift)]'
                    : 'right-0 border-l border-line-2 shadow-[-24px_0_40px_-18px_var(--lift)]',
            )}
        >
            <div className="relative flex-none h-[2.8125rem] flex items-start gap-0.5 pt-[0.9375rem] pr-2 pl-3.5 border-b border-line text-[0.75rem]">
                <span className="flex-1 text-ink-2 font-medium leading-[1.125rem]">Commands</span>
                <span role="group" aria-label="Dock side" className="inline-flex gap-0.5 mt-[0.1875rem]">
                    {(['left', 'right'] as const).map(s => {
                        const Icon = s === 'left' ? PanelLeft : PanelRight;
                        const label = s === 'left' ? 'Dock left' : 'Dock right';
                        return (
                            <button
                                key={s}
                                type="button"
                                aria-pressed={side === s}
                                aria-label={label}
                                title={label}
                                onClick={() => store().setCommandPaneSide(s)}
                                className="w-[1.375rem] h-[1.375rem] inline-flex items-center justify-center rounded-xs text-ink-3 hover:text-ink hover:bg-sheet-2 aria-pressed:text-ink aria-pressed:bg-sheet-2"
                            >
                                <Icon className="size-3.5" strokeWidth={1.6} />
                            </button>
                        );
                    })}
                </span>
            </div>
            {paneView === 'picker' && targetRow}
            {paneView === 'commands' && commandsView}
            {paneView === 'settings' && (
                <SettingsView
                    section={settingsSection}
                    desktop={desktop}
                    from={fromSection}
                    onOpen={section => store().setCommandPaneView({ kind: 'settings', section })}
                    onBack={back}
                />
            )}
            {/* The selected cell's picker renders here (CellEditor's portal)
                and brings its own head, search, list and footer. */}
            <div
                ref={slotRef}
                {...{ [PANE_PANEL_SLOT_ATTR]: '' }}
                className={hosted ? 'flex-1 min-h-0 flex flex-col' : 'hidden'}
            />
        </aside>
    );
    // Over the full-window view or the channel-creation view: above it, the
    // window's height on its side.
    return overlay ? createPortal(pane, document.body) : pane;
}
