import { resolveSceneKeyboardGroup } from './sceneKeyboardCommands';
import { executeSceneSizing, executeSceneActions, executeSceneBack, executeSceneModes, executeSceneMovement } from './sceneKeyboardEffects';
import { useEffect, useRef } from 'react';
import type { Stack } from '@/lib/types';
import { Channel } from '@/lib/types';
import { FocusContextType } from '@/contexts/FocusContext';
import { SceneActions } from '@/hooks/useSceneActions';
import {
    PendingMoveState,
    useSpatialStore,
    withSettlingMove,
} from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { is } from '@/lib/keybindings/handlers';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { isTypingTarget } from '@/lib/keybindings/match';
import { eventPathHasAttr, sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { RUNS_PANE_ATTR } from '@/lib/runsDock';
import { keysInside } from '@/lib/selectionFocus';
import { hasPendingAnnotationPick } from '@/lib/rendererAnnotation';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { inCommandPane } from '@/lib/commandPane';
import {
    FanRange, resolveCellSource, resolveCellLanding, cellMoveStops,
    stepCellMoveStop, pendingMoveTarget, landingOf, MOVE_INTO_CAPABILITIES_RUNNING,
} from '@/lib/fanOut';
import { frontCellIndexOf, frontCellOf } from '@/lib/frontSlots';
import { cellHasLiveRun } from '@/lib/runningCell';

export interface UseKeyboardHandlerParams {
    focusState: FocusContextType;
    mainStacks: Stack[];
    activeChannel: Channel | null;
    performAction: (action: Promise<unknown>) => Promise<unknown>;
    isZoomedOut: boolean;
    setIsZoomedOut: (v: boolean) => void;
    toggleZoom: () => void;
    setTransitionType: React.Dispatch<React.SetStateAction<'nav' | 'cell'>>;
    setVisibleTopIndices: React.Dispatch<React.SetStateAction<Record<string, number>>>;
    setMergeNonces: React.Dispatch<React.SetStateAction<Record<string, number>>>;
    visibleTopIndicesRef: React.MutableRefObject<Record<string, number>>;
    handleRemoveStack: (sid: string) => void;
    actions: SceneActions;
    /** Fan-out (useSceneActions): the fanned stack, its toggle, its display
     *  span, and the display list the navigation and cell-move shortcuts
     *  step over. `displaySelectedId` is the selected column
     *  (`fan-<cellId>` when the fanned stack is selected, else the real
     *  id); `selectDisplayId` resolves a display id back to the real
     *  stack + top index. */
    fannedOutStackId: string | null;
    setFannedOutStackId: (id: string | null) => void;
    fan: FanRange | null;
    displayStacks: Stack[];
    displaySelectedId: string | null;
    selectDisplayId: (id: string, cellIdx?: number) => void;
}

export function useKeyboardHandler({
    focusState, mainStacks, activeChannel,
    performAction,
    isZoomedOut, setIsZoomedOut, toggleZoom,
    setTransitionType, setVisibleTopIndices, setMergeNonces,
    visibleTopIndicesRef, handleRemoveStack, actions,

    fannedOutStackId, setFannedOutStackId, fan, displayStacks, displaySelectedId, selectDisplayId,
}: UseKeyboardHandlerParams) {
    const {
        setPendingMove, setCameraHold, setCenterRequested, setMinCellSize,
        setStackWidth, clearStackWidths, setIsMaxWidth,
        requestEdgeAlign,
    } = useSpatialStore.getState();
    const {
        focusArea, setFocusArea,
        selectedWindowId, setSelectedWindowId,
        mainFocusMode, setMainFocusMode,
    } = focusState;

    const moveReleaseKeys = useRef<Set<string>>(new Set());
    const cellSizeKeyStartRef = useRef<number | null>(null);
    // Maps a pooled frame holding the keys to the slot it is positioned
    // over: the status pane's composer may be such a frame.
    const { containerOf } = useGuestPool();
    const containerOfRef = useRef(containerOf);
    useEffect(() => { containerOfRef.current = containerOf; }, [containerOf]);

    useEffect(() => {
        // Shortcut parity helpers: mirror the UI hide/disable rules on the
        // cell header. If a button is hidden for a given cell state, the
        // matching shortcut must no-op too.
        const selectedStack = () => {
            const s = mainStacks.find(x => x.id === selectedWindowId);
            return s && s.cells.length > 0 ? s : null;
        };
        // The selected stack's front cell; null while Capabilities shows
        // its manage card, so every cell command leaves it alone.
        const getSelectedFrontCell = () => frontCellOf(selectedStack(), visibleTopIndicesRef.current);

        // The cell id of an open cell panel session (any kind) on the
        // focused stack, or null. The VISIBLE front cell's session wins —
        // that's the surface the user is looking at; other cells of the
        // stack are checked only as a fallback so a rotation between
        // opening and Esc can't strand a session.
        const selectedPickSessionCellId = (): string | null => {
            const sessions = useCollimateStore.getState().pickSessions;
            if (!sessions) return null;
            const stack = selectedStack();
            if (!stack) return null;
            const front = getSelectedFrontCell();
            if (front && sessions[String(front.id)]) return String(front.id);
            for (const c of stack.cells as Array<{ id: string }>) {
                const id = String(c.id);
                if (sessions[id]) return id;
            }
            return null;
        };

        // The selected stack's front cell when it is in history mode (the
        // same front-cell resolution as the pick-session helper above).
        const selectedHistoryCellId = (): string | null => {
            const hist = useCollimateStore.getState().cellHistory;
            const front = getSelectedFrontCell();
            return front && hist[String(front.id)] ? String(front.id) : null;
        };

        // The moving cell behind a pending cell move's source column: a
        // fan column is its cell of the real stack; a real stack moves
        // its front cell, and none while its manage card is in front.
        // Display-space in, real-space out.
        const pendingMoveSource = (sourceId: string) => {
            const frontIdx = frontCellIndexOf(displayStacks.find(s => s.id === sourceId), visibleTopIndicesRef.current);
            if (frontIdx === null) return null;
            return resolveCellSource(displayStacks, fan, sourceId, frontIdx,
                id => cellHasLiveRun(useCollimateStore.getState(), id));
        };
        // Advance a pending cell move one stop in `delta` over the display
        // columns. lib/fanOut.ts owns the stop list (outer gaps, every
        // position inside the fan, every real column but the
        // source), already filtered of no-ops.
        const advancePendingCellMove = (delta: -1 | 1) => {
            setPendingMove(prev => {
                if (!prev) return prev;
                const src = pendingMoveSource(prev.sourceId);
                if (!src) return prev;
                const next = stepCellMoveStop(cellMoveStops(displayStacks, fan, src, visibleTopIndicesRef.current), prev, delta);
                if (!next) return prev;
                return { ...prev, targetIndex: next.targetIndex, mode: next.mode, inFan: next.inFan };
            });
        };

        const handleKeyDown = async (e: KeyboardEvent) => {
            // A small surface that owns every key it receives (the stack
            // menu, the stack-name input) handles its own arrows / Enter /
            // Esc. This listener runs in the capture phase, before the
            // surface's handler, so it must not touch those events at all.
            if (sceneKeyboardOwned(e)) return;
            // Esc is someone else's while an element pick is pending (the
            // pick's own listener cancels it), and in a cell's status pane
            // (its views step back or close it; every other key reaches the
            // scene). The composer's pooled frame forwards its Esc on the
            // window, so its path never crosses the pane: the keyboard
            // holder decides.
            if (e.key === 'Escape' && (hasPendingAnnotationPick() || eventPathHasAttr(e, RUNS_PANE_ATTR)
                || keysInside(`[${RUNS_PANE_ATTR}]`, containerOfRef.current))) return;
            const pendingMove = useSpatialStore.getState().pendingMove;

            // Typing in a text field, a shadow-DOM renderer's included (the
            // assistant chat composer, schema forms): bare-key shortcuts (the
            // arrow nav, the width keys) leave the keystrokes to it.
            const isEditing = isTypingTarget(e);
            // The Commands pane's query: Ctrl-←/→ still moves the selected
            // cell from there (the pane acts on it); Alt-↑/↓ and Esc are the
            // pane's own (sceneKeyboardCommands).
            const fromCommandPane = inCommandPane(e);

            if (isEditing) {
                if (e.ctrlKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !fromCommandPane) {
                    // Allow word jumping in text editors
                    return;
                }
                if (!e.altKey && !e.metaKey && !e.ctrlKey && e.key !== 'Escape') {
                    return;
                }
            }

            if (e.key === 'Alt' || e.key === 'Control' || e.key === 'Meta' || e.key === 'Shift') return;
            const sceneFocusArea = focusArea === 'nav' && e.key !== 'Escape' ? 'main' : focusArea;
            if (sceneFocusArea !== focusArea) setFocusArea('main');

            // While a pendingMove is in flight, Alt-chord actions are
            // suppressed so they can't act mid-move. In zoom-out the
            // first Alt chord cancels the move.
            if (e.altKey && pendingMove && !is(e, SHORTCUT_IDS.NAV_CELL_MOVE_LEFT) && !is(e, SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT)) {
                if (isZoomedOut) { setPendingMove(null); return; }
                return;
            }

            const keyboardSnapshot = {
                matched: new Set(Object.values(SHORTCUT_IDS).filter(id => is(e, id))),
                isZoomedOut, sceneFocusArea, selectedWindowId, fromCommandPane,
            };
            const commandContext = {
                focusState, mainStacks, activeChannel, performAction,
                isZoomedOut, setIsZoomedOut, toggleZoom,
                setTransitionType, setVisibleTopIndices, setMergeNonces, visibleTopIndicesRef, handleRemoveStack, actions,
                fannedOutStackId, setFannedOutStackId, fan, displayStacks, displaySelectedId, selectDisplayId,
                setPendingMove, setCameraHold, setCenterRequested, setMinCellSize, setStackWidth, clearStackWidths, setIsMaxWidth, requestEdgeAlign,
                focusArea, setFocusArea, selectedWindowId, setSelectedWindowId, mainFocusMode, setMainFocusMode,
                getSelectedFrontCell, selectedHistoryCellId, selectedPickSessionCellId, cellSizeKeyStartRef, sceneFocusArea, pendingMove,
                moveReleaseKeys, advancePendingCellMove, pendingMoveSource,
                cellMoveLeftMatched: keyboardSnapshot.matched.has(SHORTCUT_IDS.NAV_CELL_MOVE_LEFT),
            };
            const sizingCommand = resolveSceneKeyboardGroup('sizing', keyboardSnapshot);
            if (sizingCommand) {
                executeSceneSizing(e, sizingCommand, commandContext);
                return;
            }

            const modesCommand = resolveSceneKeyboardGroup('modes', keyboardSnapshot);
            if (modesCommand) {
                executeSceneModes(e, modesCommand, commandContext);
                return;
            }

            const actionsCommand = resolveSceneKeyboardGroup('actions', keyboardSnapshot);
            if (actionsCommand) {
                executeSceneActions(e, actionsCommand, commandContext);
                return;
            }

            const navigationCommand = resolveSceneKeyboardGroup('navigation', keyboardSnapshot);
            if (navigationCommand) {
                executeSceneMovement(e, navigationCommand, commandContext);
                return;
            }

            const movementCommand = resolveSceneKeyboardGroup('movement', keyboardSnapshot);
            if (movementCommand) {
                executeSceneMovement(e, movementCommand, commandContext);
                return;
            }

            const backCommand = resolveSceneKeyboardGroup('back', keyboardSnapshot);
            if (backCommand) {
                executeSceneBack(e, backCommand, commandContext);
                return;
            }




        };

        const commitPendingMove = async (move: PendingMoveState) => {
            // A cell move's slots are display-space; resolve them into the
            // real move first (lib/fanOut.ts) and bail before touching the
            // camera when nothing would change.
            const src = pendingMoveSource(move.sourceId);
            const landing = src
                ? resolveCellLanding(displayStacks, fan, src, pendingMoveTarget(move, displayStacks, visibleTopIndicesRef.current))
                : null;
            if (landing?.kind === 'refused' && landing.reason === 'running') {
                void useCollimateStore.getState().openAlert({ title: 'Move refused', body: MOVE_INTO_CAPABILITIES_RUNNING });
            }
            if (landing?.kind !== 'move') return;
            const op = landing.op;
            setCameraHold(true);
            // Carry the source-stack id across the post-commit settle so
            // StackScene's iframe-z bump keeps the moving stack's renderers
            // on top of stacks it passes over. pendingMove was just cleared
            // (above, to prevent double-commit on simultaneous key
            // releases), so it can't carry this signal itself.
            setTransitionType('cell');
            window.dispatchEvent(new Event('canvas-pan-start'));
            await withSettlingMove(move.sourceId, async () => {
                const res = await actions.applyCellOp(op);
                // Selection follows the moved cell: the column it landed
                // in becomes the front cell (the selected column of a
                // fanned stack), and a merge into another stack bumps
                // that stack's merge nonce.
                const land = landingOf(res, op.cellId);
                if (land) {
                    setSelectedWindowId(land.stackId);
                    // A REAL index: the scene stores its slot (lib/frontSlots).
                    actions.winSetTopCell(land.stackId, land.cellIdx);

                    if (op.kind === 'intoStack' && land.stackId !== fannedOutStackId) {
                        setMergeNonces(prev => ({ ...prev, [land.stackId]: (prev[land.stackId] ?? 0) + 1 }));
                    }
                }
            }, () => {
                setCameraHold(false);
                window.dispatchEvent(new Event('canvas-pan-end'));
            });
        };

        const handleKeyUp = async (e: KeyboardEvent) => {
            const pendingMove = useSpatialStore.getState().pendingMove;
            // If we have a pending move, check if the required modifier keys are released
            if (pendingMove) {
                const shouldCommit = moveReleaseKeys.current.has(e.key);

                if (shouldCommit) {
                    moveReleaseKeys.current.clear();
                    const move = { ...pendingMove };
                    // Clear state immediately to prevent double-commit from simultaneous key releases
                    setPendingMove(null);
                    await commitPendingMove(move);
                }
            }
        };

        window.addEventListener('keydown', handleKeyDown, { capture: true });
        window.addEventListener('keyup', handleKeyUp, { capture: true });
        return () => {
            window.removeEventListener('keydown', handleKeyDown, { capture: true });
            window.removeEventListener('keyup', handleKeyUp, { capture: true });
        };
    }, [
        focusArea, selectedWindowId, mainFocusMode, activeChannel,
        performAction, setFocusArea, setSelectedWindowId, setMainFocusMode, handleRemoveStack,
        mainStacks, actions, isZoomedOut, setIsZoomedOut, toggleZoom,
        fannedOutStackId, setFannedOutStackId, fan, displayStacks, displaySelectedId, selectDisplayId,
        setTransitionType, setVisibleTopIndices, setMergeNonces, visibleTopIndicesRef,
        // focusState carries focusArea / selection state read
        // throughout the handler; re-register when it changes so the
        // closure doesn't act on a stale snapshot.
        focusState,
    ]);
}
