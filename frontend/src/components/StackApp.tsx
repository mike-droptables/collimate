import {withFlushedEditors} from '@/lib/rendererFlush';
import { useState, useMemo, useCallback, useEffect, useRef } from 'react';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { useAppState } from '@/hooks/useAppState';
import { ApiSettingsModal } from './ApiSettingsModal';
import * as api from '@/lib/api';
import { FocusProvider, useFocus } from '@/contexts/FocusContext';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { StackScene } from './StackScene';
import { ServerDownScreen } from './ServerDownScreen';
import { ShutdownOverlay } from './ShutdownOverlay';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { AppTopBar } from './AppTopBar';
import { useZoomActions } from '@/hooks/useZoomActions';
import { useSceneActions } from '@/hooks/useSceneActions';
import { useKeyboardHandler } from '@/hooks/useKeyboardHandler';
import { executeFindFile } from '@/hooks/sceneKeyboardEffects';
import { useChannelSSE } from '@/hooks/useChannelSSE';
import { useCanvasFileDrop } from '@/hooks/useCanvasFileDrop';
import { NewCellSlotGhost } from './NewCellSlotGhost';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { ConfirmDialog } from './ConfirmDialog';
import { CommandPane } from './CommandPane';
import { EmptyChannelNewCell } from './EmptyChannelNewCell';
import { ChannelCreateView } from './ChannelCreateView';
import { DesktopTitleBar } from './DesktopTitleBar';
import { BlurredTopBar } from './BlurredTopBar';
import { useMaxWidthTracker } from '@/hooks/useMaxWidthTracker';
import type { CreateChannelBody } from '@/lib/api';
import { findCapabilitiesStack } from '@/lib/types';
import { FAN_TOGGLE_SETTLE_MS } from '@/lib/fanOut';
import { frontCellOf } from '@/lib/frontSlots';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { PANE_QUERY_ATTR } from '@/lib/commandPane';
import { focusPaneTarget } from '@/lib/fullWindowView';
import { useCommandPanePersistence } from '@/hooks/useCommandPanePersistence';
import { useSelectionFocus } from '@/hooks/useSelectionFocus';
import { desktopBridge, desktopServers } from '@/lib/desktop';


export default function StackApp() {
    // Column layout so the desktop shell's title bar (nothing in a browser
    // tab) sits above the app rather than under it — StackAppContent's
    // roots fill the remainder via flex-1.
    return (
        <ThemeProvider>
            <FocusProvider>
                <div className="h-screen w-full flex flex-col bg-canvas text-ink">
                    <DesktopTitleBar />
                    <StackAppContent />
                </div>
            </FocusProvider>
        </ThemeProvider>
    );
}

/** While an OS file drag is over the window every desktop guest is
 *  occluded: a native guest view sits above all DOM and would take the
 *  drag (and the drop) from the Files panes and the canvas beneath it. */
function OsFileDragOcclusion() {
    const active = useCollimateStore(s => s.osFileDragActive);
    useGuestOccluder('all', active);
    return null;
}

function StackAppContent() {
    const { state, activeChannel, performAction, apiError, refresh } = useAppState();
    const focusState = useFocus();
    const isShuttingDown = useCollimateStore(s => s.isShuttingDown);
    const hasInitialized = useCollimateStore(s => s.hasInitialized);
    // Explicit "create a new channel" request (the channel-creation view
    // shown over the active channel). The view is also shown implicitly when
    // there is no active channel at all (first launch / last channel deleted).
    const [creatingChannel, setCreatingChannel] = useState(() => window.location.hash === '#new-channel');
    useEffect(() => {
        if (window.location.hash === '#new-channel') {
            window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
        }
    }, []);
    // The channel-creation view owns the screen whenever there's no active
    // channel (loading/empty) or the user explicitly asked to create one over
    // a live channel. While it does, the underlying scene's global shortcuts +
    // keyboard handler must stay dormant so e.g. Mod+K can't open the Commands pane
    // behind the overlay.
    const channelCreationActive = !activeChannel || creatingChannel;
    // What the creation view may copy from: the channels by id and name
    // (the store holds nothing more of a channel that is not open).
    const channelChoices = useMemo(() => (state?.channels ?? []).map(({ id, name }) => ({ id, name })), [state?.channels]);

    // If the active channel disappears while an explicit "new channel" request
    // is pending (e.g. it was deleted from another window), drop the request —
    // the no-channel create view takes over, and a stale flag would otherwise
    // re-open the overlay the moment a channel exists again.
    useEffect(() => {
        if (hasInitialized && !activeChannel && creatingChannel) setCreatingChannel(false);
    }, [hasInitialized, activeChannel, creatingChannel]);

    const [isApiModalOpen, setIsApiModalOpen] = useState(false);
    // Settings live in the Commands pane. File → Servers… in the desktop
    // shell opens the pane on the Servers section, with or without a channel.
    useEffect(() => {
        const off = desktopBridge()?.onShellCommand(command => {
            if (command.type === 'openServers') useCollimateStore.getState().openCommandPaneSettings('servers');
            else void useCollimateStore.getState().openAlert({ title: 'Could not stop the local server', body: command.message });
        });
        if (off) document.documentElement.setAttribute('data-shell-command-ready', '');
        return () => {
            off?.();
            document.documentElement.removeAttribute('data-shell-command-ready');
        };
    }, []);
    const { isZoomedOut, setIsZoomedOut, toggleZoom } = useZoomActions();


    const activeChannelId = activeChannel?.id;
    useChannelSSE(activeChannelId);

    // Keeps a max-width shared stack width at the full width (see the hook).
    useMaxWidthTracker();

    const mainStacks = useMemo(() => activeChannel?.main_stacks || [], [activeChannel]);
    // The channel's Capabilities stack and whether it is on the canvas
    // (else hidden to its top-bar pill).
    const capabilitiesPlacement = useMemo(() => findCapabilitiesStack(activeChannel), [activeChannel]);

    // Invariant: in the main scene (fan-out included) and zoom-out,
    // selection must land on a real stack whenever any exist. Covers cases like a cell removal
    // that leaves `selectedWindowId` pointing at a stack no longer in
    // the channel. ``prevMainStacksRef`` remembers the last layout so we
    // can resolve the vanished stack's neighbors.
    const prevMainStacksRef = useRef(mainStacks);
    useEffect(() => {
        const sel = focusState.selectedWindowId;
        const stillValid = sel && mainStacks.some(s => s.id === sel);
        if (!stillValid && mainStacks.length > 0) {
            // The selected stack vanished. Hand selection to its left
            // neighbor from the prior layout, falling back to the right,
            // then to the leftmost remaining stack.
            const prev = prevMainStacksRef.current;
            const oldIdx = prev.findIndex(s => s.id === sel);
            const candidates = [
                oldIdx > 0 ? prev[oldIdx - 1]?.id : undefined,
                oldIdx !== -1 ? prev[oldIdx + 1]?.id : undefined,
            ];
            const nextId = candidates.find(id => !!id && mainStacks.some(s => s.id === id))
                ?? mainStacks[0].id;
            focusState.setSelectedWindowId(nextId);
        }
        prevMainStacksRef.current = mainStacks;
    }, [focusState.selectedWindowId, focusState.setSelectedWindowId, mainStacks]);

    // Pending-select consumer. The run-dispatch path (and any other
    // "spawn a new cell" call site) parks a cellId on
    // ``pendingSelectCellId`` so we can wait for the matching stack
    // to materialise — a Run-in-new-cell clone doesn't appear in
    // ``mainStacks`` until the next state push. Once it shows up, set selection to its
    // stack, request a centre, and clear the pending marker.
    const pendingSelectCellId = useCollimateStore(s => s.pendingSelectCellId);
    const pendingSelectFocus = useCollimateStore(s => s.pendingSelectFocus);
    const clearPendingSelectCell = useCollimateStore(s => s.clearPendingSelectCell);
    // (The consumer effect sits below `scene`: it writes visibleTopIndices.)

    // Mod+, (Mac convention) opens the Commands pane on the settings root,
    // whatever the pane showed; Esc steps back out of it there.
    useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS, (e) => {
        e.preventDefault();
        useCollimateStore.getState().openCommandPaneSettings(null);
    }, { target: 'window', enabled: !channelCreationActive });

    const scene = useSceneActions({ performAction, focusState, mainStacks, activeChannel, isZoomedOut });

    // A drop onto bare canvas makes a new cell: OS files (a load-vs-file
    // prompt for .coll* bundles) or a cell file dragged off every file
    // browser. A cell's Files pane takes the drags headed into that cell
    // itself, so they never reach the canvas. The new cell's stack takes the
    // selection, like every add the user makes; the fan maps the drop slot
    // to a main_stacks index.
    const selectCreatedStack = useCallback((stackId: string) => {
        focusState.setFocusArea('main');
        focusState.setMainFocusMode('window');
        focusState.setSelectedWindowId(stackId);
    }, [focusState]);
    const canvasDrop = useCanvasFileDrop(activeChannelId, selectCreatedStack, scene.fanRange);

    // Pending-select consumer: once the awaited cell's stack materialises,
    // select the stack, bring that cell to the front (the selected column
    // of a fanned stack; the visible cell of a normal one) and clear the
    // marker. It never recentres the camera (owner, 2026-10-13): the
    // camera's selection follow scrolls only as far as it takes to bring
    // the stack into view; centring is the explicit centre command's. A
    // request with `focus` (an annotation pick) also gives the canvas the
    // main focus area, so the cell is the focused window and its view takes
    // the pick's click instead of its focus blocker.
    const sceneActions = scene.actions;
    useEffect(() => {
        if (!pendingSelectCellId) return;
        const stack = mainStacks.find(s =>
            s.cells.some(c => String(c.id) === pendingSelectCellId),
        );
        if (!stack) return; // wait for next state tick
        if (pendingSelectFocus) focusState.setFocusArea('main');
        focusState.setSelectedWindowId(stack.id);
        sceneActions.winSetTopCell(stack.id, stack.cells.findIndex(c => String(c.id) === pendingSelectCellId));
        clearPendingSelectCell();
    }, [pendingSelectCellId, pendingSelectFocus, mainStacks, focusState, clearPendingSelectCell, sceneActions]);

    // Mirror the selected cell (the front cell of the selected column) into
    // the store, so a run started elsewhere opens its card focused only on
    // the cell the user is looking at. Null while Capabilities shows its
    // manage card, which owns its own focus (it is no cell).
    const selectedColumn = scene.displayStacks.find(s => s.id === scene.displaySelectedId);
    const selectedFront = frontCellOf(selectedColumn, scene.visibleTopIndices);
    const selectedCellId = selectedFront ? String(selectedFront.id) : null;
    useEffect(() => {
        useCollimateStore.getState().setSelectedCellId(selectedCellId);
    }, [selectedCellId]);
    // The keyboard follows the selection: focus left in another cell
    // moves to this one, its editor when the keys were in an editor
    // (lib/selectionFocus).
    useSelectionFocus(selectedCellId, isZoomedOut);

    // The Commands pane: the top bar's Commands button and Mod+K toggle it
    // (Mod+K also from inside the pane's query, so it closes from there),
    // a bare `/` opens it, or puts the keyboard back in its query (from the
    // settings view, back on the commands). Its state
    // is per channel, kept in the channel's settings document.
    useCommandPanePersistence();
    const commandsOpen = useCollimateStore(s => s.commandPane.open);
    // Closing hands the keyboard to the selected cell, or to the full-window
    // view's content while it is open (the pane's Esc does too). The pane
    // lies over the full-window view as over the canvas (CommandPane).
    const toggleCommands = () => {
        const store = useCollimateStore.getState();
        store.toggleCommandPane();
        if (!store.commandPane.open) return;
        focusPaneTarget(selectedCellId);
    };

    useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE, (e) => {
        if (sceneKeyboardOwned(e)) return;
        e.preventDefault();
        toggleCommands();
    }, { target: 'window', enabled: !channelCreationActive, skipWhenTyping: false });

    // Mod+P (default, remappable): Find file, the Files panel on the
    // selected cell (also from inside a cell panel or a renderer).
    useShortcut(SHORTCUT_IDS.GLOBAL_FIND_FILE, (e) => {
        executeFindFile(e, { selectedCellId, isZoomedOut });
    }, { target: 'window', enabled: !channelCreationActive, skipWhenTyping: false });

    useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS, (e) => {
        if (sceneKeyboardOwned(e)) return;
        e.preventDefault();
        const store = useCollimateStore.getState();
        if (!store.commandPane.open) store.openCommandPane();
        else if (store.commandPane.view.kind !== 'commands') store.setCommandPaneView({ kind: 'commands' });
        else document.querySelector<HTMLInputElement>(`[${PANE_QUERY_ATTR}]`)?.focus({ preventScroll: true });
    }, { target: 'window', enabled: !channelCreationActive });

    // A fan-out toggle unmounts the real StackGroup (AnimatePresence exit)
    // and mounts fan columns under new motion.div keys, or the reverse on
    // collapse. Two failure modes drop renderers visually:
    //  1) New SandboxIframes' IntersectionObserver fires its initial
    //     observation before motion.div has applied its target transform,
    //     capturing a wrong rect, leaving isVisible=false until something
    //     (a pan, a scroll) jolts the observer.
    //  2) Old SandboxIframes' iframes — which live in the world-space
    //     overlay (z-110), not under the motion.div — can stay positioned
    //     over their previous cell during the exit fade, "floating" over
    //     the new fanned layout.
    // Broadcast a re-evaluate signal at two moments: the next frame
    // (catches the just-committed layout for newly-mounted cells) and
    // once the toggle has settled (catches the post-unmount truth for
    // cells that are gone). Listeners re-observe / re-position; the bumps
    // are idempotent.
    useEffect(() => {
        const dispatch = () => window.dispatchEvent(new CustomEvent('collimate:scene-invalidated'));
        const raf = requestAnimationFrame(dispatch);
        const timeout = window.setTimeout(dispatch, FAN_TOGGLE_SETTLE_MS);
        return () => {
            cancelAnimationFrame(raf);
            clearTimeout(timeout);
        };
    }, [scene.fannedOutStackId]);

    // The Capabilities pill: hide the stack to the top bar, or show it right
    // of the client's selected stack (the server's `main_selection_id` can
    // lag the client, so the anchor is explicit) and select it. Its front
    // slot is the one it last had (useSceneActions keeps it by id): the
    // manage card the first time.
    // The selection invariant above resolves a successfully hidden stack's
    // neighbor from the prior layout. A refused hide keeps that layout and
    // selection intact; it must not perform a second selection write.
    const toggleCapabilitiesStack = useCallback(async () => {
        if (!capabilitiesPlacement || !activeChannelId) return;
        const { stack, shown } = capabilitiesPlacement;
        if (shown) {
            await performAction(api.minimizeStack(activeChannelId, stack.id));
            return;
        }
        // Await the restore so mainStacks includes the stack before we set
        // selection. Without this await, the "selection must land on a real
        // main stack" invariant effect sees selectedWindowId pointing at a
        // nav-only stack and bounces it to mainStacks[0].
        const restored = await performAction(api.restoreMinimizedStack(activeChannelId, stack.id, focusState.selectedWindowId));
        if (restored === null) return;
        focusState.setFocusArea('main');
        focusState.setSelectedWindowId(stack.id);
    }, [performAction, capabilitiesPlacement, focusState.selectedWindowId, focusState.setFocusArea, focusState.setSelectedWindowId, activeChannelId]);

    // A Capabilities manage request (any door: Commands, the stack menu,
    // the Files panel's 'Show in Capabilities', the empty runner picker):
    // show the stack (restoring it when hidden), select it and front its
    // manage card in the main view. The card applies the payload and
    // clears the request (this effect clears it when the restore fails);
    // each request is shown once.
    const capabilitiesManageRequest = useCollimateStore(s => s.capabilitiesManageRequest);
    const shownManageSeqRef = useRef<number | null>(null);
    useEffect(() => {
        if (!capabilitiesManageRequest || !capabilitiesPlacement || !activeChannelId) return;
        if (shownManageSeqRef.current === capabilitiesManageRequest.seq) return;
        const { seq } = capabilitiesManageRequest;
        shownManageSeqRef.current = seq;
        const { stack, shown } = capabilitiesPlacement;
        void (async () => {
            if (!shown) {
                const restored = await performAction(api.restoreMinimizedStack(activeChannelId, stack.id, focusState.selectedWindowId));
                // Nothing opened: the request goes, so a later restore by
                // another door does not apply it.
                if (restored === null) { useCollimateStore.getState().clearCapabilitiesManageRequest(seq); return; }
            }
            focusState.setFocusArea('main');
            focusState.setSelectedWindowId(stack.id);
            sceneActions.winShowManage(stack.id);
            setIsZoomedOut(false);
        })();
    }, [capabilitiesManageRequest, capabilitiesPlacement, activeChannelId, performAction, focusState, sceneActions, setIsZoomedOut]);

    const handleZoomToCell = useCallback((sid: string, idx: number) => {
        focusState.setFocusArea('main');
        focusState.setSelectedWindowId(sid);

        scene.actions.winSetTopCell(sid, idx);
        setIsZoomedOut(false);
    }, [focusState.setFocusArea, focusState.setSelectedWindowId, scene.actions, setIsZoomedOut]);

    useKeyboardHandler({
        focusState, mainStacks, activeChannel,
        performAction,
        isZoomedOut, setIsZoomedOut, toggleZoom,
        setTransitionType: scene.setTransitionType,
        setVisibleTopIndices: scene.setVisibleTopIndices,
        setMergeNonces: scene.setMergeNonces,
        visibleTopIndicesRef: scene.visibleTopIndicesRef,
        handleRemoveStack: scene.handleRemoveStack,
        actions: scene.actions,
        fannedOutStackId: scene.fannedOutStackId,
        setFannedOutStackId: scene.setFannedOutStackId,
        fan: scene.fanRange,
        displayStacks: scene.displayStacks,
        displaySelectedId: scene.displaySelectedId,
        selectDisplayId: scene.selectDisplayId,
    });


    const handleSelectChannel = useCallback(async (cid: string) => {
        try {
            await withFlushedEditors(()=>api.selectChannel(cid));
            await refresh();
        } catch (e) {
            console.error('Select channel failed:', e);
        }
    }, [refresh]);

    // A channel on another server: the shell selects it there and recreates
    // the window on that server's origin, so this page's editors save first.
    const handleSelectServerChannel = useCallback(async (serverId: string, cid: string) => {
        const servers = desktopServers();
        if (!servers) return;
        try {
            await withFlushedEditors(() => servers.openChannel(serverId, cid));
        } catch (e) {
            void useCollimateStore.getState().openAlert({
                title: 'Could not open that channel', body: e instanceof Error ? e.message : String(e),
            });
        }
    }, []);

    const handleCreateChannel = useCallback(async (name: string, body: CreateChannelBody) => {
        // Let errors propagate so ChannelCreateView can re-enable its Create
        // button. Refresh to the new (now active) channel first, then drop the
        // create overlay — so it lifts straight onto the new channel rather
        // than flashing the previously-active scene.
        await withFlushedEditors(()=>api.createChannel(name, body));
        await refresh();
        setCreatingChannel(false);
    }, [refresh]);

    const handleRenameChannel = useCallback(async (name: string) => {
        if (!activeChannel) return;
        try {
            await api.renameChannel(activeChannel.id, name);
            await refresh();
        } catch (e) {
            // The server's refusal (a live run, a taken name) is the user's
            // to act on, so it is shown, not only logged.
            void useCollimateStore.getState().openAlert({
                title: 'Rename failed', body: api.describeApiError(e),
            });
        }
    }, [activeChannel, refresh]);

    const handleDeleteChannel = useCallback(async () => {
        if (!activeChannel) return;
        try {
            await api.deleteChannel(activeChannel.id);
            await refresh();
        } catch (e) {
            void useCollimateStore.getState().openAlert({
                title: 'Delete failed', body: api.describeApiError(e),
            });
        }
    }, [activeChannel, refresh]);

    // Shutdown takes priority over every other view. Once a `shutdown`
    // SSE event fires, the server is intentionally going away — we don't
    // want to flash ServerDownScreen while teardown runs.
    if (isShuttingDown) {
        return <ShutdownOverlay />;
    }

    if (!activeChannel) {
        if (apiError) {
            return (
                <>
                    <ServerDownScreen onOpenSettings={() => setIsApiModalOpen(true)} />
                    <ApiSettingsModal isOpen={isApiModalOpen} onClose={() => setIsApiModalOpen(false)} />
                </>
            );
        }

        // Still doing the first /api/state fetch — don't flash the create
        // view before we know whether any channel exists.
        if (!hasInitialized) {
            return (
                <div className="flex flex-1 min-h-0 items-center justify-center flex-col gap-4 text-ink-3 bg-canvas">
                    <div className="text-[0.84375rem] font-medium animate-pulse text-ink-2">Loading...</div>
                </div>
            );
        }

        // No channel exists (first launch, or the last channel was deleted).
        // The channel-creation view is the sole surface — no Cancel, nothing
        // to return to — over a blurred, unusable top bar.
        return (
            <div className="flex-1 min-h-0 w-full overflow-hidden bg-canvas text-ink relative">
                <BlurredTopBar />
                <ChannelCreateView onCreate={handleCreateChannel} channels={[]} sourceChannelId={null} />
                <ApiSettingsModal isOpen={isApiModalOpen} onClose={() => setIsApiModalOpen(false)} />
                <ConfirmDialog />
                {/* The Commands pane without a scene, over the creation view:
                    File → Servers… opens it on Servers, since a server with
                    no channel must still let the user pick another. */}
                <CommandPane scene={null} />
            </div>
        );
    }

    return (
        <div className="flex-1 min-h-0 w-full flex flex-col overflow-hidden bg-canvas text-ink relative">
            {/* The top bar sits in the page flow above the canvas, the same
                row in the main view and zoom-out. `inert` while the
                channel-creation view covers it: the blurred stand-in
                (BlurredTopBar) replaces it visually and carries the one live
                control (the title-bar restore icon), so the covered bar must
                leave the focus order and the a11y tree too. */}
            <div className="relative z-30 flex-none" inert={creatingChannel || undefined}>
                <AppTopBar
                    activeChannel={activeChannel}
                    channels={state?.channels || [activeChannel]}
                    columns={scene.displayStacks}
                    selectedColumnId={scene.displaySelectedId}
                    visibleTopIndices={scene.visibleTopIndices}
                    fanName={scene.fanRange?.name ?? null}
                    onSelectColumn={scene.selectDisplayId}
                    isZoomedOut={isZoomedOut}
                    toggleZoom={toggleZoom}
                    onNewStack={() => scene.actions.newEmptyStackRight(focusState.selectedWindowId)}
                    commandsOpen={commandsOpen}
                    onCommands={toggleCommands}
                    capabilitiesStack={capabilitiesPlacement?.stack ?? null}
                    capabilitiesShown={!!capabilitiesPlacement?.shown}
                    onToggleCapabilities={toggleCapabilitiesStack}
                    onSelectChannel={handleSelectChannel}
                    onSelectServerChannel={handleSelectServerChannel}
                    onRequestNewChannel={() => setCreatingChannel(true)}
                    onRenameChannel={handleRenameChannel}
                    onDeleteChannel={handleDeleteChannel}
                    onOpenSettings={() => useCollimateStore.getState().openCommandPaneSettings(null)}
                />
            </div>

            <div className="relative flex-1 min-h-0">
                {/* Scene layer — one StackScene fed displayStacks: the fanned
                    stack (if any) is replaced in place by `fan-<cellId>`
                    single-cell columns; selection stays a real stack id plus
                    visibleTopIndices. Upstream state the user cares about —
                    pane/tab (useCollimateStore), visible-top indices and merge
                    nonces (useSceneActions), focus/selection (FocusContext),
                    camera/widths (useSpatialStore) — all live above StackScene and
                    survive the toggle. */}
                <div className="absolute inset-0 z-0" {...canvasDrop.dragProps}>
                    {canvasDrop.isDragging && <NewCellSlotGhost pointerX={canvasDrop.pointerX} fan={scene.fanRange} />}
                    <OsFileDragOcclusion />
                    <StackScene
                        stacks={scene.displayStacks}
                        selectedId={scene.displaySelectedId}
                        actions={scene.actions}
                        transitionType={scene.transitionType}
                        topology={scene.topology}
                        isZoomedOut={isZoomedOut}
                        visibleTopIndices={scene.visibleTopIndices}
                        mergeNonces={scene.mergeNonces}
                        fan={scene.fanRange}
                        fanAnchor={scene.fanAnchor}
                        onFanOut={scene.setFannedOutStackId}
                        onCollapseFan={scene.collapseFan}
                        onSelectStack={scene.selectDisplayId}
                        onZoomToCell={handleZoomToCell}
                    />
                </div>

                <EmptyChannelNewCell
                    isEmpty={mainStacks.length === 0}
                    isZoomedOut={isZoomedOut}
                    onNew={() => scene.actions.newEmptyStackRight(null)}
                />

                {/* The Commands pane, docked over the canvas's edge; over
                    the channel-creation view it has no scene and sits above
                    the view, so File → Servers… still opens it on Servers. */}
                <CommandPane
                    scene={creatingChannel ? null : {
                        displayStacks: scene.displayStacks,
                        displaySelectedId: scene.displaySelectedId,
                        visibleTopIndices: scene.visibleTopIndices,
                        fanRange: scene.fanRange,
                        selectDisplayId: scene.selectDisplayId,
                        setFannedOutStackId: scene.setFannedOutStackId,
                        actions: scene.actions,
                        mainStacks,
                        selectedStackId: focusState.selectedWindowId,
                        isZoomedOut,
                        toggleZoom,
                        capabilities: {
                            shown: !!capabilitiesPlacement?.shown,
                            // Hide it, or show it on its manage card.
                            toggle: capabilitiesPlacement?.shown
                                ? toggleCapabilitiesStack
                                : () => useCollimateStore.getState().requestCapabilitiesManage({}),
                            create: () => useCollimateStore.getState().requestCapabilitiesManage({ create: true }),
                        },
                    }}
                />
            </div>

            <ConfirmDialog />

            <ApiSettingsModal isOpen={isApiModalOpen} onClose={() => setIsApiModalOpen(false)} />

            {/* Create-a-new-channel surface, requested from the channel menu
                while a channel is active. Same blank main space as the
                no-channel state (opaque, covering the scene) with a blurred,
                unusable top bar — plus a Cancel back to the active channel. */}
            {creatingChannel && (
                <>
                    <BlurredTopBar />
                    <ChannelCreateView
                        onCreate={handleCreateChannel}
                        onCancel={() => setCreatingChannel(false)}
                        channels={channelChoices}
                        sourceChannelId={activeChannel.id}
                    />
                </>
            )}
        </div>
    );
}
