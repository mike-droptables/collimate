// The stack-width operators every width surface shares: the channel's
// "all stacks" preference, its saved default width ("Set current"), and
// the max toggle.
// The max toggle is ONE code path for the head's double-click (StackGroup),
// the Width tool's Max (StackWidthControl) and Alt+F (sceneKeyboardEffects).

import { DEFAULT_MIN_CELL_SIZE, useSpatialStore } from '@/stores/useSpatialStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { putChannelSettings } from '@/lib/api';
import { commandPaneInsetOf, type ViewInset } from '@/lib/commandPane';
import { MIN_STACK_WIDTH, maxStackWidthFor } from '@/hooks/useStackLayout';

type Spatial = ReturnType<typeof useSpatialStore.getState>;
type SettingsDoc = ReturnType<typeof useChannelSettingsStore.getState>['doc'];

/** The channel's "all stacks" preference: on unless the channel turned it off. */
export const allStacksOn = (s: Spatial, channelId: string | null | undefined): boolean =>
    channelId ? (s.allStacksByChannel[channelId] ?? true) : true;

/** The channel's saved default width (the Width tool's "Set current"), or
 *  the app default (DEFAULT_MIN_CELL_SIZE). */
export function channelDefaultWidth(doc: SettingsDoc = useChannelSettingsStore.getState().doc): number {
    const w = doc?.layout?.defaultStackWidth;
    return typeof w === 'number' && w > 0 ? w : DEFAULT_MIN_CELL_SIZE;
}

/** The full stack width: the widest a stack fits in the canvas the
 *  Commands pane leaves while it is open (the pane pushes the content out
 *  from under it). Every max and every clamp the width surfaces apply. */
export function fullStackWidth(
    viewWidth: number = window.innerWidth,
    inset: ViewInset = commandPaneInsetOf(useCollimateStore.getState().commandPane),
): number {
    return maxStackWidthFor(viewWidth - inset.left - inset.right);
}

/** A column's width as the layout draws it, unclamped. */
const columnWidth = (s: Spatial, columnId: string | null): number =>
    (columnId ? s.stackWidths[columnId] : undefined) ?? s.minCellSize;

/** Whether the max toggle reads as on for this column: the shared max
 *  flag ("all stacks" on), the column's own max flag, or the column already
 *  as wide as the viewport allows. */
export function isMaxWidthOn(columnId: string | null, channelId: string | null | undefined): boolean {
    const s = useSpatialStore.getState();
    const max = fullStackWidth();
    if (allStacksOn(s, channelId) && s.isMaxWidth) return true;
    if (!columnId && !allStacksOn(s, channelId)) return false;
    // Maximised on its own: its flag marks it (the tracker keeps it at the
    // full width as the pane opens and closes).
    if (columnId && !allStacksOn(s, channelId) && s.maxColumns[columnId]) return true;
    return Math.min(columnWidth(s, columnId), max) >= max - 1;
}

/** The Width tool's "Set current": store ``width`` as the channel's default
 *  (the layout section's ``defaultStackWidth``), the width Reset and
 *  un-maximising return to. The server replaces the section whole, so the
 *  save carries its other keys (the Commands pane's saved state); with the
 *  channel's settings not loaded there is no section to carry, and nothing
 *  is stored. */
export function setChannelDefaultWidth(
    width: number,
    channelId: string | undefined = useCollimateStore.getState().activeChannel?.id,
): void {
    const store = useChannelSettingsStore.getState();
    if (!channelId || !store.doc || store.loadedFor !== channelId) return;
    const layout = { ...store.doc.layout, defaultStackWidth: width };
    store.patchDoc(channelId, { layout });
    void putChannelSettings(channelId, { layout })
        .catch(() => { void store.reloadAfterExternalWrite(); });
}

/** The max toggle. Maximising first stores the width the column had as
 *  the channel's default (``setChannelDefaultWidth``, the Width tool's
 *  "Set current"); un-maximising returns to the channel default, so to
 *  that width. A column already full stores nothing: the toggle reads it
 *  as on and takes it to the default.
 *
 *  "All stacks" on: the shared width. Maximising turns on the max flag
 *  (the viewport tracker keeps it glued to the screen) and drops every
 *  per-stack override. Off: only this column (a display id, fan columns
 *  included), marked in ``maxColumns``. With "all stacks" off and no
 *  column there is nothing to toggle. */
export function toggleMaxWidth(columnId: string | null, channelId: string | null | undefined): void {
    const s = useSpatialStore.getState();
    const shared = allStacksOn(s, channelId);
    if (!shared && !columnId) return;
    const max = fullStackWidth();
    if (isMaxWidthOn(columnId, channelId)) {
        const back = Math.max(MIN_STACK_WIDTH, Math.min(channelDefaultWidth(), max));
        if (shared) {
            s.setMinCellSize(back);
            s.setIsMaxWidth(false);
            s.clearStackWidths();
        } else if (columnId) {
            // The write drops the column's max flag (setStackWidth).
            s.setStackWidth(columnId, back);
        }
        return;
    }
    setChannelDefaultWidth(Math.max(MIN_STACK_WIDTH, Math.min(columnWidth(s, columnId), max)));
    if (shared) {
        s.setMinCellSize(max);
        s.setIsMaxWidth(true);
        s.clearStackWidths();
    } else if (columnId) {
        s.setStackWidth(columnId, max);
        s.setColumnMax(columnId, true);
    }
}
