import type { MutableRefObject } from 'react';
import type { UseKeyboardHandlerParams } from './useKeyboardHandler';
import type { FocusContextType } from '@/contexts/FocusContext';
import { useSpatialStore, type PendingMoveState } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { closeHistoryTabs } from '@/lib/cellLayout';
import { focusPaneTarget } from '@/lib/fullWindowView';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { cellHasLiveRun } from '@/lib/runningCell';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { MIN_STACK_WIDTH } from './useStackLayout';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import { platformModifiers } from '@/lib/keybindings/match';
import { cellMoveStops, firstCellMoveStop, type resolveCellSource } from '@/lib/fanOut';
import { frontCellIndexOf } from '@/lib/frontSlots';
import { isCapabilitiesStack } from '@/lib/types';
import { allStacksOn, channelDefaultWidth, fullStackWidth, toggleMaxWidth } from '@/lib/stackWidth';

type Context = UseKeyboardHandlerParams & Pick<ReturnType<typeof useSpatialStore.getState>,
    'setPendingMove' | 'setCameraHold' | 'setCenterRequested' | 'setMinCellSize' | 'setStackWidth' |
    'clearStackWidths' | 'setIsMaxWidth' | 'requestEdgeAlign'> &
    Pick<FocusContextType, 'focusArea' | 'setFocusArea' | 'selectedWindowId' | 'setSelectedWindowId' | 'mainFocusMode' | 'setMainFocusMode'> & {
        getSelectedFrontCell: () => UseKeyboardHandlerParams['mainStacks'][number]['cells'][number] | null;
        selectedHistoryCellId: () => string | null;
        selectedPickSessionCellId: () => string | null;
        cellSizeKeyStartRef: MutableRefObject<number | null>;
        sceneFocusArea: FocusContextType['focusArea'];
        pendingMove: PendingMoveState | null;
        moveReleaseKeys: MutableRefObject<Set<string>>;
        advancePendingCellMove: (delta: -1 | 1) => void;
        pendingMoveSource: (sourceId: string) => ReturnType<typeof resolveCellSource>;
        cellMoveLeftMatched: boolean;
    };

export function executeSceneSizing(e: KeyboardEvent, command: string, ctx: Context) {
            // Width shortcuts (Alt+= / Alt+- / Alt+0) — always active,
            // even in zoom-out. They mirror the per-cell width handle:
            // when a channel's "all stacks" toggle is on they adjust the
            // shared width (and drop every per-stack override — sliding
            // to max is a manual pick, so the max-track flag clears);
            // when off they adjust only the selected column. Target =
            // the display selection: the ctx.fan column when the fanned
            // stack is selected, else the focused stack.
            const widthTargetId = ctx.displaySelectedId;
            const channelId = ctx.activeChannel?.id;
            const applyWidth = (next: number) => {
                const sp = useSpatialStore.getState();
                const max = fullStackWidth();
                const clamped = Math.max(MIN_STACK_WIDTH, Math.min(next, max));
                if (allStacksOn(sp, channelId)) {
                    ctx.setMinCellSize(clamped);
                    ctx.clearStackWidths();
                    if (sp.isMaxWidth) ctx.setIsMaxWidth(false);
                } else if (widthTargetId) {
                    ctx.setStackWidth(widthTargetId, clamped);
                }
            };
            const currentWidth = () => {
                const sp = useSpatialStore.getState();
                return allStacksOn(sp, channelId)
                    ? sp.minCellSize
                    : (widthTargetId ? (sp.stackWidths[widthTargetId] ?? sp.minCellSize) : sp.minCellSize);
            };
            if (command === SHORTCUT_IDS.SCENE_WIDTH_INC) {
                e.preventDefault();
                if (!e.repeat) ctx.cellSizeKeyStartRef.current = Date.now();
                const step = e.repeat && ctx.cellSizeKeyStartRef.current && Date.now() - ctx.cellSizeKeyStartRef.current >= 500 ? 100 : 50;
                applyWidth(currentWidth() + step);
                return;
            }
            if (command === SHORTCUT_IDS.SCENE_WIDTH_DEC) {
                e.preventDefault();
                if (!e.repeat) ctx.cellSizeKeyStartRef.current = Date.now();
                const step = e.repeat && ctx.cellSizeKeyStartRef.current && Date.now() - ctx.cellSizeKeyStartRef.current >= 500 ? 100 : 50;
                applyWidth(currentWidth() - step);
                return;
            }
            if (command === SHORTCUT_IDS.SCENE_WIDTH_RESET) {
                e.preventDefault();
                ctx.cellSizeKeyStartRef.current = null;
                // The channel default, as the Width tool's Reset.
                applyWidth(channelDefaultWidth());
                return;
            }
            // Max-width toggle (Alt+F) — works in the main view, ctx.fan
            // columns included (not zoom-out). The one max operator the
            // head's double-click and the Width tool's Max share
            // (lib/stackWidth toggleMaxWidth): maximising stores the width
            // before as the channel default, and un-maximising returns to it.
            if (!ctx.isZoomedOut && command === SHORTCUT_IDS.SCENE_FULLSCREEN) {
                e.preventDefault();
                toggleMaxWidth(widthTargetId ?? null, channelId);
                return;
            }


}

export function executeSceneActions(e: KeyboardEvent, command: string, ctx: Context) {
            // Below: shortcuts that only apply outside zoom-out.
            if (!ctx.isZoomedOut) {
                // 'New cell' sits above the selection guard because it works
                // even with no selection (empty channel) — parity with the
                // top-bar '+' button, which passes null and falls back to
                // createStack.
                if (ctx.sceneFocusArea === 'main' && command === SHORTCUT_IDS.STACK_NEW_CELL) {
                    e.preventDefault();
                    ctx.actions.newEmptyStackRight(ctx.selectedWindowId ?? null);
                    return;
                }
                // New cell + run options: same create, then the runners
                // panel opens on the fresh cell.
                if (ctx.sceneFocusArea === 'main' && command === SHORTCUT_IDS.STACK_NEW_CELL_RUN) {
                    e.preventDefault();
                    void ctx.actions.newEmptyStackRight(ctx.selectedWindowId ?? null).then(res => {
                        if (res) useCollimateStore.getState().openPickSession(res.cellId, 'runners');
                    });
                    return;
                }

                // Main-scene stack ctx.actions — parity: the matching button
                // must exist and be enabled on the focused cell header
                // for the shortcut to fire.
                if (ctx.sceneFocusArea === 'main' && ctx.selectedWindowId) {
                    const stack = ctx.mainStacks.find(s => s.id === ctx.selectedWindowId);
                    // Nothing runs on Capabilities, it never fans out or is
                    // duplicated as a stack, and it is hidden, never removed.
                    const selectedIsCapabilities = isCapabilitiesStack(stack);
                    const frontCell = ctx.getSelectedFrontCell();
                    // Removal is blocked while a run is live — on the front
                    // cell for CELL_REMOVE, on any cell of the stack for
                    // STACK_REMOVE — parity with the hidden remove button and
                    // the greyed stack-menu item (a run's files are its lock).
                    // Duplicates are always allowed: the copy leaves the
                    // runs' files behind.
                    const store = useCollimateStore.getState();
                    const cellLive = !!frontCell && cellHasLiveRun(store, String(frontCell.id));
                    const stackLive = !!stack && stack.cells.some(c => cellHasLiveRun(store, String(c.id)));
                    // The selected stack's front cell index (its selected
                    // column while fanned out); null while Capabilities
                    // shows its manage card, where no cell command acts.
                    const frontIdx = frontCellIndexOf(stack, ctx.visibleTopIndicesRef.current);
                    // Non-null while the selected stack is the fanned one:
                    // the selected column's cell index.
                    const fanColumn = ctx.fan && ctx.fan.stackId === ctx.selectedWindowId ? frontIdx : null;

                    if (command === SHORTCUT_IDS.STACK_RUN) {
                        e.preventDefault();
                        // No run starts on a Capabilities cell (backend 400)
                        // — mirror its head, which has no Run.
                        if (selectedIsCapabilities) return;
                        // Live runs never block it: several runs may share
                        // a cell. The runners panel opens on the display column (the selected
                        // ctx.fan column, or the stack itself).
                        useCollimateStore.getState().requestOpenGeneratorRunner(ctx.displaySelectedId ?? ctx.selectedWindowId);
                        return;
                    }
                    // Duplicate — main view: the front cell into a fresh stack
                    // beside it. Fan column: in place; the copy becomes the
                    // next column and is selected (any stack type).
                    if (command === SHORTCUT_IDS.STACK_DUPLICATE) {
                        e.preventDefault();
                        if (fanColumn !== null) {
                            void ctx.actions.duplicateCellInStack(ctx.selectedWindowId, fanColumn);
                            return;
                        }
                        ctx.actions.winDuplicateToNewStack(ctx.selectedWindowId);
                        return;
                    }
                    // Duplicate + run options: the copy lands, then the
                    // runners panel opens on it. Not from Capabilities.
                    if (command === SHORTCUT_IDS.STACK_DUPLICATE_RUN) {
                        e.preventDefault();
                        if (selectedIsCapabilities) return;
                        if (fanColumn !== null) {
                            void ctx.actions.duplicateCellInStack(ctx.selectedWindowId, fanColumn).then(res => {
                                if (res) useCollimateStore.getState().openPickSession(res.cellId, 'runners');
                            });
                            return;
                        }
                        void ctx.actions.winDuplicateToNewStack(ctx.selectedWindowId).then(res => {
                            if (res) useCollimateStore.getState().openPickSession(res.cellId, 'runners');
                        });
                        return;
                    }
                    // Duplicate stack follows the stack menu's Duplicate, which
                    // Capabilities does not offer.
                    if (command === SHORTCUT_IDS.STACK_DUPLICATE_STACK) {
                        e.preventDefault();
                        if (selectedIsCapabilities) return;
                        ctx.actions.duplicateStack(ctx.selectedWindowId);
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_CENTER) {
                        e.preventDefault();
                        ctx.setCenterRequested(prev => prev + 1);
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_ALIGN_LEFT) {
                        e.preventDefault();
                        ctx.requestEdgeAlign('left');
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_ALIGN_RIGHT) {
                        e.preventDefault();
                        ctx.requestEdgeAlign('right');
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_REMOVE) {
                        e.preventDefault();
                        // Capabilities hides to its pill (nothing runs there).
                        if (!selectedIsCapabilities && stackLive) return;
                        ctx.handleRemoveStack(ctx.selectedWindowId);
                        return;
                    }
                    if (command === SHORTCUT_IDS.CELL_REMOVE) {
                        e.preventDefault();
                        // On Capabilities, its manage card included, the
                        // cell's Remove is Hide.
                        if (selectedIsCapabilities) { ctx.handleRemoveStack(ctx.selectedWindowId); return; }
                        if (cellLive || frontIdx === null) return;
                        // Remove the currently-visible (front) cell of the
                        // selected stack — the selected column while fanned
                        // out. If this is a stack's only cell, the backend
                        // drops the stack too — same semantic as clicking the
                        // X on the cell header. The action keeps the fanned
                        // stack's selected column in place.
                        ctx.actions.winRemoveCell(ctx.selectedWindowId, frontIdx);
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_ROTATE_PREV) {
                        e.preventDefault();
                        ctx.actions.winRotate(ctx.selectedWindowId, -1);
                        return;
                    }
                    if (command === SHORTCUT_IDS.STACK_ROTATE_NEXT) {
                        e.preventDefault();
                        ctx.actions.winRotate(ctx.selectedWindowId, 1);
                        return;
                    }

                }
            }


}

export function executeSceneBack(e: KeyboardEvent, command: string, ctx: Context) {
            if (command === SHORTCUT_IDS.GLOBAL_BACK) {
                const store = useCollimateStore.getState();
                e.preventDefault();
                if (ctx.pendingMove) { ctx.setPendingMove(null); return; }
                // The selected stack's visible cell is in a cell-pair diff:
                // Esc exits that diff. Stack view only — zoom-out shows no
                // diff, so Esc there must not end one the user can't see.
                if (!ctx.isZoomedOut) {
                    const front = ctx.getSelectedFrontCell();
                    const frontId = front ? String(front.id) : null;
                    if (frontId && store.cellDiffs[frontId]) {
                        store.exitCellDiff(frontId);
                        return;
                    }
                }
                // History mode on the selected stack's front cell: Esc backs
                // out of nested file-version scrubbing first, then exits
                // history (sweeping its read-only tabs), then falls through.
                // While a maximized-tab overlay is up, defer entirely — its
                // own EDITOR_CLOSE collapses it; a second Esc reaches here.
                if (!ctx.isZoomedOut && !store.maximizedOverlayCellId) {
                    const histCellId = ctx.selectedHistoryCellId();
                    const hist = histCellId ? store.cellHistory[histCellId] : undefined;
                    if (histCellId && hist) {
                        if (hist.versionId !== null) {
                            store.setCellHistory(histCellId, { versionId: null });
                            return;
                        }
                        store.closeCellHistory(histCellId);
                        store.updateCellLayout(histCellId, l => closeHistoryTabs(l));
                        return;
                    }
                }
                // An open Commands pane closes next (Esc typed in the pane
                // is the pane's own), handing the keyboard to the cell: the
                // full-window view's content while that view is open (the
                // card lies under it), the card otherwise. The Esc ends
                // here: the cell's file editor (its window-level Esc closes
                // the file's tab) never sees it.
                if (store.commandPane.open) {
                    e.stopPropagation();
                    store.closeCommandPane();
                    const front = ctx.getSelectedFrontCell();
                    focusPaneTarget(front ? String(front.id) : null);
                    return;
                }
                // Esc outside the panel dismisses the focused cell's
                // panel before it touches ctx.fan-out / zoom / focus — but only
                // where the panel actually renders: not in zoom-out
                // (the overview never mounts CellEditor, so closing there
                // would delete an invisible session instead of exiting
                // the mode).
                if (!ctx.isZoomedOut) {
                    const pickCellId = ctx.selectedPickSessionCellId();
                    if (pickCellId) {
                        store.closePickSession(pickCellId, store.pickSessions[pickCellId].kind);
                        return;
                    }
                }
                // A fanned-out stack collapses next — whichever stack is
                // selected.
                if (ctx.fannedOutStackId) { ctx.setFannedOutStackId(null); return; }
                if (ctx.isZoomedOut) { ctx.setIsZoomedOut(false); return; }
                if (ctx.mainFocusMode !== 'window') { ctx.setMainFocusMode('window'); return; }
                ctx.setFocusArea('nav');
                return;
            }
}


export function executeSceneModes(e: KeyboardEvent, command: string, ctx: Context) {
            if (command === SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM) {
                e.preventDefault();
                ctx.toggleZoom();
                return;
            }

            // Fan-out toggle (Alt+V by default) for the focused stack: collapse
            // it when it is the fanned one, else ctx.fan it out (replacing any
            // other ctx.fan) when it has ≥ 2 cells. Esc is the collapse-whatever
            // key; this one only ever acts on the focused stack. An Alt chord
            // during a pending move is swallowed above, so no toggle mid-move.
            if (command === SHORTCUT_IDS.SCENE_FAN_OUT) {
                e.preventDefault();
                if (ctx.isZoomedOut || ctx.sceneFocusArea !== 'main' || !ctx.selectedWindowId) return;
                if (ctx.fannedOutStackId === ctx.selectedWindowId) { ctx.setFannedOutStackId(null); return; }
                const s = ctx.mainStacks.find(x => x.id === ctx.selectedWindowId);
                // Capabilities never fans out.
                if (s && !isCapabilitiesStack(s) && s.cells.length >= 2) ctx.setFannedOutStackId(ctx.selectedWindowId);
                return;
            }


}

export function executeSceneMovement(e: KeyboardEvent, command: string, ctx: Context) {
            // Cell navigation (default Ctrl+Arrow), in the stack view and the
            // overview alike. Each direction is a separate id so remaps keep
            // the left/right pairing intact. Steps across DISPLAY columns, so
            // entering a fanned group lands on its first/last column.
            const isNavLeft = command === SHORTCUT_IDS.NAV_STACK_LEFT;
            const isNavRight = command === SHORTCUT_IDS.NAV_STACK_RIGHT;
            if (isNavLeft || isNavRight) {
                e.preventDefault();
                const delta = isNavRight ? 1 : -1;

                if (ctx.pendingMove) {
                    if (ctx.isZoomedOut) { ctx.setPendingMove(null); return; }
                    ctx.advancePendingCellMove(delta);
                    return;
                }

                if (ctx.sceneFocusArea === 'main' && ctx.displaySelectedId) {
                    const i = ctx.displayStacks.findIndex(s => s.id === ctx.displaySelectedId);
                    const t = i + delta;
                    if (i >= 0 && t >= 0 && t < ctx.displayStacks.length) ctx.selectDisplayId(ctx.displayStacks[t].id);
                }
                return;
            }

            // Cell move (default Ctrl+Shift+Arrow). Starts or advances a
            // ctx.pendingMove over the display columns; commit fires on key-up
            // in handleKeyUp. The source is the selected column (a ctx.fan
            // column moves its cell; a stack moves its front cell).
            const isCellMoveLeft = ctx.cellMoveLeftMatched;
            const isCellMoveRight = command === SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT;
            if (isCellMoveLeft || isCellMoveRight) {
                if (ctx.isZoomedOut) return;
                e.preventDefault();
                const delta = isCellMoveRight ? 1 : -1;
                if (ctx.sceneFocusArea !== 'main' || !ctx.displaySelectedId) return;

                if (ctx.pendingMove) {
                    ctx.advancePendingCellMove(delta);
                    return;
                }
                const moveBinding = useKeybindingsStore.getState().resolve(isCellMoveLeft ? SHORTCUT_IDS.NAV_CELL_MOVE_LEFT : SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT);
                if (!moveBinding) return;
                const modifiers = platformModifiers(moveBinding);
                ctx.moveReleaseKeys.current = new Set([
                    ...(modifiers.meta ? ['Meta'] : []), ...(modifiers.ctrl ? ['Control'] : []),
                    ...(moveBinding.alt ? ['Alt'] : []), ...(moveBinding.shift ? ['Shift'] : []),
                ]);
                if (!ctx.moveReleaseKeys.current.size) return;
                const srcIdx = ctx.displayStacks.findIndex(s => s.id === ctx.displaySelectedId);
                if (srcIdx < 0) return;
                const d = ctx.displayStacks[srcIdx];
                const src = ctx.pendingMoveSource(d.id);
                if (!src) return;
                const stops = cellMoveStops(ctx.displayStacks, ctx.fan, src, ctx.visibleTopIndicesRef.current);
                const first = firstCellMoveStop(stops, srcIdx, delta);
                // No stop in that direction (edge, or a cell that can't
                // move at all) → no pending move starts.
                if (!first) return;
                ctx.setPendingMove({
                        type: 'cell',
                    sourceId: d.id,
                    originalIndex: srcIdx,
                    targetIndex: first.targetIndex,
                    mode: first.mode,
                    inFan: first.inFan,
                });
                return;
            }


}


/** The cell whose panel holds the keyboard (the event came from inside a
 *  `[data-cell-panel]`, or one has the focus), else null. */
function panelCellOf(e: Event): string | null {
    const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
    for (const node of path) {
        if (node instanceof Element && node.hasAttribute('data-cell-panel')) return node.getAttribute('data-pick-cell');
    }
    return document.activeElement?.closest('[data-cell-panel]')?.getAttribute('data-pick-cell') ?? null;
}

/** Find file (Mod+P, D-K02): open the Files panel (row P03) on the cell
 *  whose panel holds the keyboard, else on the selected cell. Never the
 *  browser's print dialog. Not in the overview (no cell editor mounts
 *  there), not over a modal or another keyboard owner. */
export function executeFindFile(e: KeyboardEvent, ctx: { selectedCellId: string | null; isZoomedOut: boolean }) {
    e.preventDefault();
    if (ctx.isZoomedOut) return;
    const panelCell = panelCellOf(e);
    if (!panelCell && sceneKeyboardOwned(e)) return;
    const cellId = panelCell ?? ctx.selectedCellId;
    if (cellId) openFilesPanel(cellId);
}

/** Open the cell's Files panel (row P03), for Mod+P, the Commands pane's
 *  Find file… and an empty tab's button: a Files panel already up takes
 *  the keyboard into its search, and a cell in version history leaves it
 *  first. Not on a cell in a cell diff (read-only: no panel shows on it). */
export function openFilesPanel(cellId: string) {
    const store = useCollimateStore.getState();
    if (store.cellDiffs[cellId]) return;
    if (store.pickSessions[cellId]?.kind === 'files') {
        document.querySelector<HTMLInputElement>(`[data-cell-panel="files"][data-pick-cell=${JSON.stringify(cellId)}] input`)?.focus({ preventScroll: true });
        return;
    }
    // A cell in version history takes no panel: leave history first, as
    // the files rail's Find file does (and as Esc does).
    if (store.cellHistory[cellId]) {
        store.closeCellHistory(cellId);
        store.updateCellLayout(cellId, l => closeHistoryTabs(l));
    }
    store.openPickSession(cellId, 'files');
}
