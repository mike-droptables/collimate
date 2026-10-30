// The Commands pane's command list (the mock's `commands()`): every row the
// pane can show for the selected cell, grouped (GROUP_ORDER) Recent / This
// cell / Stack / Channel / Settings / Go to / Keyboard. Pure: the pane builds
// a snapshot of the scene and hands it here, so the list is testable without
// the canvas. Every registry
// shortcut is a row: carried by the command that does the same, else a
// Keyboard row that fires it. A row's binding (`bindingOf`) is its
// shortcut's combo, or the combo the person bound to the command key.

import type { LucideIcon } from 'lucide-react';
import {
    AlignLeft, ArrowDown, ArrowDownUp, ArrowUp, Boxes, File, FoldHorizontal, Keyboard, LayoutGrid, MessageSquare, PackagePlus,
    PanelLeft, PanelRight, Plus, RotateCcw, Settings, Shrink, Split, Square, TextSearch, UnfoldHorizontal,
} from 'lucide-react';
import { PlayGlyph } from '@/components/ui/PlayGlyph';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { allShortcuts, SHORTCUT_REGISTRY } from '@/lib/keybindings/registry';
import { fireShortcut } from '@/lib/keybindings/fire';
import { UNIVERSAL_COMBOS } from '@/lib/keybindings/commandBindings';
import { displayComboString } from '@/lib/keybindings/display';
import type { KeybindingsState } from '@/lib/keybindings/store';
import { SHORTCUT_CONTEXT_LABEL, type ShortcutContext, type ShortcutId } from '@/lib/keybindings/types';
import type { CommandPaneSide, SettingsSection } from '@/stores/collimate/commandPaneSlice';
import { settingsSectionsFor } from '@/components/commandPane/settingsSections';
import type { FilesPaneSide, RunsPaneSide } from '@/stores/collimate/types';
import { FILES_PANE_SIDES, RUNS_PANE_SIDES } from '@/lib/cellLayout';
import { FILES_SIDE_ICON } from '@/lib/filesDock';
import { RUNS_SIDE_ICON } from '@/lib/runsDock';

export type CommandGroup = 'Recent' | 'This cell' | 'Stack' | 'Channel' | 'Settings' | 'Go to' | 'Keyboard';

/** The groups in the order the pane lists them: each group shows once, its
 *  rows together, so the list reads in the order ↑↓ walk it. */
const GROUP_ORDER: readonly CommandGroup[] = ['Recent', 'This cell', 'Stack', 'Channel', 'Settings', 'Go to', 'Keyboard'];

export interface PaneCommand {
    /** Stable across renders: the recents remember it. */
    key: string;
    group: CommandGroup;
    icon: LucideIcon;
    label: string;
    /** Quiet text on the right (a stack name, a count). */
    meta?: string;
    /** What the command does, at length: the row's tooltip, and the
     *  search matches it (a registry shortcut's description). */
    description?: string;
    /** The registry shortcut that does the same: the row's binding is its
     *  combo (a row without one binds through its command key). */
    shortcut?: ShortcutId;
    /** The command puts the keyboard somewhere itself (a hosted picker,
     *  the settings view, a surface outside the pane, which closes the
     *  pane first): the pane leaves focus where the command put it. */
    leavesPane?: boolean;
    run: () => void;
}

/** What the commands act on and the state their labels read. */
export interface CommandSnapshot {
    zoomedOut: boolean;
    side: CommandPaneSide;
    /** Inside the desktop shell: the desktop-only settings sections show. */
    desktop: boolean;
    target: {
        cellId: string;
        stackId: string;
        stackName: string;
        /** The cell sits on Capabilities: nothing runs there, so it has no
         *  run, log, runs or status-dock rows. */
        capabilities: boolean;
        /** A built-in card: it never imports or exports. */
        builtin: boolean;
        /** The cell editor is mounted for it (not zoomed out). */
        editor: {
            /** The cell's status pane; null while its layout is unseeded. */
            statusPane: { open: boolean; side: RunsPaneSide } | null;
            /** null: the cell has no files pane yet (its layout is unseeded). */
            filesShown: boolean | null;
            /** The pane's dock side; null with filesShown. */
            filesSide: FilesPaneSide | null;
            /** Tabs in the cell other than the open one, one per file. */
            otherTabs: { id: string; path: string; name: string }[];
        } | null;
        liveRuns: number;
        /** The front cell declares `.options_stack` and no run is live on it. */
        canSplit: boolean;
        cellCount: number;
        frontIdx: number;
        fanned: boolean;
    } | null;
    /** null: no channel is open (the pane over the channel-creation
     *  view), which offers only the dock row and the Settings rows. */
    channel: {
        /** Capabilities is on the canvas (else hidden to the top bar). */
        capabilitiesShown: boolean;
        runningHere: number;
        runningElsewhere: number;
    } | null;
    /** Every other column on the canvas (a fanned stack's columns each
     *  count), by its front cell; cellId null for Capabilities while its
     *  manage card is in front (the row is titled 'Capabilities'). */
    visibleCells: { columnId: string; cellId: string | null; title: string; stackName: string }[];
}

export interface CommandHandlers {
    openRunners: () => void;
    findFile: () => void;
    /** The cell's transfer panel; it stays in the cell, so the pane closes. */
    openTransfer: () => void;
    /** The status pane on All's Log (the Commands pane closes: the log
     *  takes the keyboard). */
    showLog: () => void;
    /** The status pane on its status view (the Commands pane closes: the
     *  view takes the keyboard). */
    openStatus: () => void;
    /** The status pane on All with its composer taking the keyboard. */
    annotate: () => void;
    openTab: (tabId: string) => void;
    setFilesShown: (shown: boolean) => void;
    /** Dock the Files pane on a side and show it. */
    dockFiles: (side: FilesPaneSide) => void;
    /** Dock the status pane on a side and open it. */
    dockStatus: (side: RunsPaneSide) => void;
    stepCell: (dir: -1 | 1) => void;
    setFanned: (fanned: boolean) => void;
    split: () => void;
    /** Hide Capabilities, or show it on its manage card. */
    toggleCapabilitiesStack: () => void;
    /** Open Capabilities on its Create questionnaire. */
    createCapability: () => void;
    /** The top bar's runs menu (the pane closes: it takes the keyboard). */
    openRunsMenu: () => void;
    toggleZoom: () => void;
    dock: (side: CommandPaneSide) => void;
    newStack: () => void;
    /** The pane's settings view: the root (null) or one section. */
    openSettings: (section: SettingsSection | null) => void;
    goTo: (columnId: string) => void;
    /** Reset every shortcut and command binding, after a confirm. */
    resetShortcuts: () => void;
}

export function buildPaneCommands(s: CommandSnapshot, h: CommandHandlers): PaneCommand[] {
    const out: PaneCommand[] = [];
    const other: CommandPaneSide = s.side === 'right' ? 'left' : 'right';
    const dock: PaneCommand = { key: 'dock', group: 'Channel', icon: other === 'left' ? PanelLeft : PanelRight, label: `Dock this pane ${other}`, run: () => h.dock(other) };
    const settings: PaneCommand[] = settingsSectionsFor(s.desktop).map(section => ({
        key: `settings:${section.id}`, group: 'Settings', icon: section.icon, label: section.label,
        leavesPane: true, run: () => h.openSettings(section.id),
    }));
    if (!s.channel) return [dock, ...settings];
    const t = s.target;
    if (t) {
        const ed = t.editor;
        // Nothing runs on Capabilities: its cells have no run or status rows.
        if (ed && !t.capabilities) {
            out.push({ key: 'run', group: 'This cell', icon: PlayGlyph, label: 'Run…', meta: 'runners', shortcut: SHORTCUT_IDS.STACK_RUN, leavesPane: true, run: h.openRunners });
            out.push({ key: 'log', group: 'This cell', icon: AlignLeft, label: 'Show the log', leavesPane: true, run: h.showLog });
            out.push({ key: 'cellruns', group: 'This cell', icon: Square, label: 'Runs in this cell…', meta: `${t.liveRuns} running`, leavesPane: true, run: h.openStatus });
            out.push({ key: 'annotate', group: 'This cell', icon: MessageSquare, label: 'Write an annotation…', leavesPane: true, run: h.annotate });
        }
        if (ed) {
            out.push({ key: 'findfile', group: 'This cell', icon: TextSearch, label: 'Find file…', shortcut: SHORTCUT_IDS.GLOBAL_FIND_FILE, leavesPane: true, run: h.findFile });
            if (!t.builtin) out.push({ key: 'transfer', group: 'This cell', icon: ArrowDownUp, label: 'Import / export…', leavesPane: true, run: h.openTransfer });
            for (const tab of ed.otherTabs) {
                out.push({ key: `tab:${tab.path}`, group: 'This cell', icon: File, label: `Open tab · ${tab.name}`, run: () => h.openTab(tab.id) });
            }
            if (ed.filesShown !== null && ed.filesSide !== null) {
                const shown = ed.filesShown;
                out.push({ key: 'files', group: 'This cell', icon: FILES_SIDE_ICON[ed.filesSide], label: shown ? 'Hide files' : 'Show files', run: () => h.setFilesShown(!shown) });
                // The keyboard's route to the dock sides (the icon's
                // right-click and drag are the pointer's).
                for (const side of FILES_PANE_SIDES) {
                    out.push({ key: `dockfiles:${side}`, group: 'This cell', icon: FILES_SIDE_ICON[side], label: `Dock files on the ${side}`, run: () => h.dockFiles(side) });
                }
            }
            if (ed.statusPane && !t.capabilities) {
                // The keyboard's route to the status pane's sides (the
                // caret's right-click and drag are the pointer's).
                for (const side of RUNS_PANE_SIDES) {
                    out.push({ key: `dockstatus:${side}`, group: 'This cell', icon: RUNS_SIDE_ICON[side], label: `Dock status on the ${side}`, run: () => h.dockStatus(side) });
                }
            }
        }
        // On Capabilities the first cell steps back to the manage card.
        if (!t.fanned && (t.frontIdx > 0 || t.capabilities)) {
            out.push({ key: 'prev', group: 'Stack', icon: ArrowUp, label: 'Previous cell in stack', meta: t.stackName, shortcut: SHORTCUT_IDS.STACK_ROTATE_PREV, run: () => h.stepCell(-1) });
        }
        if (!t.fanned && t.frontIdx < t.cellCount - 1) {
            out.push({ key: 'next', group: 'Stack', icon: ArrowDown, label: 'Next cell in stack', meta: t.stackName, shortcut: SHORTCUT_IDS.STACK_ROTATE_NEXT, run: () => h.stepCell(1) });
        }
        if (t.cellCount > 1 && !s.zoomedOut && !t.capabilities) {
            out.push({
                key: 'fan', group: 'Stack', icon: t.fanned ? FoldHorizontal : UnfoldHorizontal,
                label: t.fanned ? 'Collapse' : 'Fan out', meta: t.stackName, shortcut: SHORTCUT_IDS.SCENE_FAN_OUT,
                run: () => h.setFanned(!t.fanned),
            });
        }
        if (t.canSplit) {
            out.push({ key: 'split', group: 'Stack', icon: Split, label: 'Split', meta: t.stackName, run: h.split });
        }
    }
    out.push({
        key: 'capabilities', group: 'Channel', icon: Boxes,
        label: s.channel.capabilitiesShown ? 'Hide Capabilities' : 'Show Capabilities', run: h.toggleCapabilitiesStack,
    });
    out.push({
        key: 'capabilities-create', group: 'Channel', icon: PackagePlus, label: 'Create a capability…',
        leavesPane: true, run: h.createCapability,
    });
    out.push({
        key: 'running', group: 'Channel', icon: AlignLeft, label: 'Running…',
        meta: `${s.channel.runningHere} running (${s.channel.runningElsewhere})`, leavesPane: true, run: h.openRunsMenu,
    });
    out.push({
        key: 'zoom', group: 'Channel', icon: s.zoomedOut ? Shrink : LayoutGrid,
        label: s.zoomedOut ? 'Zoom back in' : 'Zoom out to the overview', shortcut: SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM, run: h.toggleZoom,
    });
    out.push(dock);
    out.push({ key: 'newstack', group: 'Channel', icon: Plus, label: 'New stack', shortcut: SHORTCUT_IDS.STACK_NEW_CELL, run: h.newStack });
    out.push({ key: 'settings', group: 'Channel', icon: Settings, label: 'Settings…', shortcut: SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS, leavesPane: true, run: () => h.openSettings(null) });
    out.push(...settings);
    for (const c of s.visibleCells) {
        out.push({
            key: c.cellId === null ? 'goto:capabilities' : `goto:${c.cellId}`, group: 'Go to', icon: c.cellId === null ? Boxes : Square,
            label: c.title, meta: c.stackName, run: () => h.goTo(c.columnId),
        });
    }
    // Every registry shortcut no row above carries, fired as if pressed. The
    // shortcut's own handler decides where the keyboard goes.
    const carried = new Set(out.flatMap(c => (c.shortcut ? [c.shortcut] : [])));
    for (const entry of allShortcuts()) {
        if (carried.has(entry.id)) continue;
        out.push({
            key: `shortcut:${entry.id}`, group: 'Keyboard', icon: Keyboard, label: entry.label,
            meta: SHORTCUT_CONTEXT_LABEL[entry.context], shortcut: entry.id, leavesPane: true,
            run: () => { fireShortcut(entry.id); },
        });
    }
    out.push({ key: 'resetshortcuts', group: 'Keyboard', icon: RotateCcw, label: 'Reset every shortcut to its default', leavesPane: true, run: h.resetShortcuts });
    // A registry-backed row carries its shortcut's description.
    return out.map(c => {
        const description = c.shortcut ? SHORTCUT_REGISTRY[c.shortcut]?.description : undefined;
        return description ? { ...c, description } : c;
    });
}

/** What a row's binding is read from: the registry and its overrides, and
 *  the command bindings (useKeybindingsStore's state). */
export type BindingsView = Pick<KeybindingsState, 'overrides' | 'commands' | 'resolveString'>;

/** A row's binding, as a canonical combo string. */
export interface RowBinding {
    combo: string;
    /** `default`: the registry's; `override`: a rebound registry shortcut;
     *  `command`: a combo bound to the command key. */
    source: 'default' | 'override' | 'command';
}

/** A row's binding: a registry-backed row's effective combo, any other
 *  row's command binding; null while unbound. */
export function bindingOf(cmd: PaneCommand, keys: BindingsView): RowBinding | null {
    if (cmd.shortcut) {
        const combo = keys.resolveString(cmd.shortcut);
        if (!combo) return null;
        return { combo, source: keys.overrides[cmd.shortcut] ? 'override' : 'default' };
    }
    const combo = keys.commands[cmd.key];
    return combo ? { combo, source: 'command' } : null;
}

/** A row's context for clashes: its shortcut's, or `global` for a command
 *  binding (it fires wherever the pane's bindings listen). */
function contextOf(cmd: PaneCommand): ShortcutContext {
    return cmd.shortcut ? (SHORTCUT_REGISTRY[cmd.shortcut]?.context ?? 'global') : 'global';
}

/** The labels of the other rows bound to `cmd`'s combo where both can fire:
 *  the same context, or either one global; the universal Escape / Enter /
 *  Shift+Enter clash only within one context. `all` is the built list (a
 *  row repeated under Recent counts once). */
export function bindingConflicts(all: PaneCommand[], cmd: PaneCommand, keys: BindingsView): string[] {
    const mine = bindingOf(cmd, keys);
    if (!mine) return [];
    const context = contextOf(cmd);
    const universal = UNIVERSAL_COMBOS.has(mine.combo);
    const seen = new Set([cmd.key]);
    const out: string[] = [];
    for (const other of all) {
        if (seen.has(other.key)) continue;
        seen.add(other.key);
        if (bindingOf(other, keys)?.combo !== mine.combo) continue;
        const otherContext = contextOf(other);
        const clash = universal
            ? otherContext === context
            : otherContext === context || otherContext === 'global' || context === 'global';
        if (clash) out.push(other.label);
    }
    return out;
}

/** The rows in GROUP_ORDER; a group keeps its rows' order (a stable sort). */
const inGroupOrder = (rows: PaneCommand[]): PaneCommand[] =>
    rows.sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group));

/** The rows the pane shows for a query (the mock's `render()`), in
 *  GROUP_ORDER: with a query, the commands whose label, meta or binding (its
 *  canonical form, `alt+r`, or as displayed, `⌥R`) contain it,
 *  case-insensitive, then within the same group those whose description
 *  alone does; without one, the recents first under "Recent", then the
 *  rest. A command shows once. */
export function visiblePaneCommands(
    all: PaneCommand[], query: string, recents: readonly string[], keys: BindingsView,
): PaneCommand[] {
    const q = query.trim().toLowerCase();
    if (q) {
        const has = (text: string | undefined) => !!text && text.toLowerCase().includes(q);
        const named = all.filter(c => {
            if (has(c.label) || has(c.meta)) return true;
            const combo = bindingOf(c, keys)?.combo;
            return !!combo && (has(combo) || has(displayComboString(combo)));
        });
        // A match in a description alone ranks after its group's other matches.
        const described = all.filter(c => !named.includes(c) && has(c.description));
        return inGroupOrder([...named, ...described]);
    }
    const recent = recents
        .map(key => all.find(c => c.key === key))
        .filter((c): c is PaneCommand => !!c)
        .map(c => ({ ...c, group: 'Recent' as const }));
    const recentKeys = new Set(recent.map(c => c.key));
    return inGroupOrder([...recent, ...all.filter(c => !recentKeys.has(c.key))]);
}

/** The label split around the first match of the query (bolded in the row). */
export function matchParts(label: string, query: string): [string, string, string] {
    const q = query.trim().toLowerCase();
    const i = q ? label.toLowerCase().indexOf(q) : -1;
    if (i < 0) return [label, '', ''];
    return [label.slice(0, i), label.slice(i, i + q.length), label.slice(i + q.length)];
}
