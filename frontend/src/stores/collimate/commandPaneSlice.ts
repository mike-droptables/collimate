import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';

/** The Commands pane's dock side: flat against the canvas's left or right edge. */
export type CommandPaneSide = 'left' | 'right';

/** The pane's width in CSS px; the camera keeps the selected cell clear
 *  of the same width the pane draws. */
export const COMMAND_PANE_WIDTH = 352;

/** Recent commands kept (the mock's Recent group). */
export const COMMAND_PANE_RECENTS = 3;

/** A section of the pane's settings view. */
export type SettingsSection = 'keys' | 'editing' | 'theme' | 'servers';

/** What the pane shows: its command list, or its settings (the root's
 *  section list while `section` is null, else that section). */
export type PaneView = { kind: 'commands' } | { kind: 'settings'; section: SettingsSection | null };

const COMMANDS_VIEW: PaneView = { kind: 'commands' };

/** The Commands pane's state. All but `view` is persisted per channel in
 *  the settings document's `layout.commandPane` (persistedCommandPane);
 *  the view is session state and starts on the commands. */
export interface CommandPaneState {
    open: boolean;
    side: CommandPaneSide;
    query: string;
    /** Command keys, most recent first, at most COMMAND_PANE_RECENTS. */
    recents: string[];
    view: PaneView;
}

/** The part of the pane's state a channel's settings document keeps. */
export type PersistedCommandPane = Omit<CommandPaneState, 'view'>;

export const defaultCommandPane = (): CommandPaneState => ({
    open: false, side: 'left', query: '', recents: [], view: COMMANDS_VIEW,
});

/** The pane's state as the settings document saves it (no view). */
export function persistedCommandPane(state: CommandPaneState): PersistedCommandPane {
    const { open, side, query, recents } = state;
    return { open, side, query, recents };
}

export interface CommandPaneSlice {
    commandPane: CommandPaneState;

    /** Open the pane (its query and view keep what they had). */
    openCommandPane: () => void;
    /** Open the pane on its settings view: the root's section list
     *  (null) or one section. */
    openCommandPaneSettings: (section: SettingsSection | null) => void;
    /** Close the pane; the query clears and the view returns to the
     *  commands (the mock's closePane). */
    closeCommandPane: () => void;
    toggleCommandPane: () => void;
    setCommandPaneView: (view: PaneView) => void;
    setCommandPaneSide: (side: CommandPaneSide) => void;
    setCommandPaneQuery: (query: string) => void;
    /** Put a command key first in the recents. */
    recordCommandPaneRecent: (key: string) => void;
    /** Replace the pane's state with a channel's saved one (its settings
     *  document loaded), on the commands view. */
    adoptCommandPane: (state: PersistedCommandPane) => void;

    /** The pane's panel slot while the pane is mounted: the element a
     *  hosted cell panel (paneHostsPanel) renders into. */
    commandPanePanelSlot: HTMLElement | null;
    setCommandPanePanelSlot: (slot: HTMLElement | null) => void;

    // A surface the pane's commands open that lives in another component
    // reads its flag here (Running…), so the pane, the component's own
    // control and the keyboard open the same surface. A cell's status pane
    // (its runs and its log) is in the cell's layout (layoutSlice).

    /** The top bar's runs menu is open. */
    runsMenuOpen: boolean;
    setRunsMenuOpen: (open: boolean) => void;
}

/** The pane shut: the query cleared and the view back on the commands. */
const closed = (pane: CommandPaneState): CommandPaneState => ({ ...pane, open: false, query: '', view: COMMANDS_VIEW });

export const createCommandPaneSlice: StateCreator<CollimateState, [], [], CommandPaneSlice> = (set) => ({
    commandPane: defaultCommandPane(),

    openCommandPane: () => set(prev => (prev.commandPane.open ? {} : { commandPane: { ...prev.commandPane, open: true } })),

    openCommandPaneSettings: (section) => set(prev => ({
        commandPane: { ...prev.commandPane, open: true, view: { kind: 'settings', section } },
    })),

    closeCommandPane: () => set(prev => (!prev.commandPane.open && prev.commandPane.query === '' && prev.commandPane.view.kind === 'commands'
        ? {}
        : { commandPane: closed(prev.commandPane) })),

    toggleCommandPane: () => set(prev => ({
        commandPane: prev.commandPane.open ? closed(prev.commandPane) : { ...prev.commandPane, open: true },
    })),

    setCommandPaneView: (view) => set(prev => ({ commandPane: { ...prev.commandPane, view } })),

    setCommandPaneSide: (side) => set(prev => (prev.commandPane.side === side ? {} : { commandPane: { ...prev.commandPane, side } })),

    setCommandPaneQuery: (query) => set(prev => (prev.commandPane.query === query ? {} : { commandPane: { ...prev.commandPane, query } })),

    recordCommandPaneRecent: (key) => set(prev => {
        const recents = [key, ...prev.commandPane.recents.filter(k => k !== key)].slice(0, COMMAND_PANE_RECENTS);
        return { commandPane: { ...prev.commandPane, recents } };
    }),

    adoptCommandPane: (state) => set({
        commandPane: {
            open: state.open, side: state.side, query: state.query,
            recents: state.recents.slice(0, COMMAND_PANE_RECENTS), view: COMMANDS_VIEW,
        },
    }),

    commandPanePanelSlot: null,
    setCommandPanePanelSlot: (slot) => set(prev => (prev.commandPanePanelSlot === slot ? {} : { commandPanePanelSlot: slot })),

    runsMenuOpen: false,
    setRunsMenuOpen: (open) => set(prev => (prev.runsMenuOpen === open ? {} : { runsMenuOpen: open })),
});
