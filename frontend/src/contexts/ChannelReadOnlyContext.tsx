/**
 * ChannelReadOnlyContext — "the open channel is archived".
 *
 * StackApp provides it from the active channel's snapshot `archived` flag
 * (plan 1105 S6, P7). An archived channel opens read-only: the server
 * refuses every edit and every run, so the window disables the controls
 * that would ask. StackItem reads it into its `readOnly` (and through
 * CellEditor's `locked` the Files pane, its context menu, the repository
 * chip, the status section's Run and the annotation pane's composers); StackScene drops the
 * drag, rename, move, duplicate and merge callbacks; StackGroup's menu
 * keeps only what reads; the top bar disables New stack and the
 * Capabilities toggle; the Capabilities manage view keeps search, Copy
 * and Publish (both read the channel and write elsewhere) and disables the
 * rest. Outside a provider nothing is read-only.
 */

import { createContext, useContext } from 'react';

export const ChannelReadOnlyContext = createContext(false);

export function useChannelReadOnly(): boolean {
    return useContext(ChannelReadOnlyContext);
}
