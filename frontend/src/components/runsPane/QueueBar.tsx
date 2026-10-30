import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { GripVertical } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { QUEUE_BAR, queueTokens, reorderQueue, type QueueToken, type Thread } from '@/lib/annotations';
import { claimKeyboardForGesture } from '@/lib/keyboardOwner';

/** A press moves this far before it is a drag. */
const DRAG_THRESHOLD = 4;

export interface QueueSectionProps {
    cellId: string;
    contextId: string;
    /** The unread messages above the bar, then those held below it, each in
     *  position order. */
    unheld: readonly Thread[];
    held: readonly Thread[];
    /** The messages on screen (Current file's filter); all when absent. A
     *  move among them orders the whole queue (lib/annotations reorderQueue). */
    shown?: ReadonlySet<string>;
    barOn: boolean;
    readOnly: boolean;
    /** One message of the queue, with its drag handle (undefined while
     *  read-only). */
    render: (thread: Thread, handle: ReactNode) => ReactNode;
}

interface Drag {
    id: string;
    /** The pointer's start and whether it has lifted into a drag. */
    x: number;
    y: number;
    lifted: boolean;
    /** The drop slot: the index among the other rows and the bar. */
    slot: number;
    handle: HTMLElement;
    pointerId: number;
}

/**
 * A run's queue: the unread messages it has not pulled, in the order it
 * will read them, then (with the queue bar on) the bar and the messages
 * held below it, which it does not see at all. A message moves by its drag
 * handle (a pointer drag, or ↑↓ on the focused handle), across the bar
 * too; Esc cancels a drag. Under a filter (`shown`) it moves among the
 * messages shown, and the hidden ones keep their places.
 */
export function QueueSection({ cellId, contextId, unheld, held, shown, barOn, readOnly, render }: QueueSectionProps) {
    const rootRef = useRef<HTMLDivElement>(null);
    const [drag, setDrag] = useState<Drag | null>(null);
    const dragRef = useRef<Drag | null>(null);
    const releaseRef = useRef<(() => void) | null>(null);
    const all = useMemo(() => queueTokens(unheld.map(t => t.root.id), held.map(t => t.root.id), barOn), [unheld, held, barOn]);
    const tokens = useMemo(() => (shown ? all.filter(t => t === QUEUE_BAR || shown.has(t)) : all), [all, shown]);
    const threads = useMemo(() => new Map([...unheld, ...held].map(t => [t.root.id, t])), [unheld, held]);
    const tokensRef = useRef({ all, tokens });
    useEffect(() => { tokensRef.current = { all, tokens }; }, [all, tokens]);

    const reorder = (id: string, slot: number) => {
        const order = reorderQueue(tokensRef.current.all, id, slot, tokensRef.current.tokens);
        if (order) void useCollimateStore.getState().reorderContextQueue(cellId, contextId, order.ids, order.heldFrom);
    };

    // The drag's window listeners, for as long as a press is down.
    useEffect(() => {
        if (!drag) return;
        const finish = (drop: boolean) => {
            const d = dragRef.current;
            dragRef.current = null;
            setDrag(null);
            releaseRef.current?.();
            releaseRef.current = null;
            if (d?.lifted && d.handle.hasPointerCapture?.(d.pointerId)) d.handle.releasePointerCapture(d.pointerId);
            if (drop && d?.lifted) reorder(d.id, d.slot);
        };
        const slotAt = (id: string, y: number) => {
            const slots = Array.from(rootRef.current?.querySelectorAll<HTMLElement>('[data-queue-slot]') ?? [])
                .filter(el => el.dataset.queueSlot !== id);
            return slots.filter(el => {
                const r = el.getBoundingClientRect();
                return r.top + r.height / 2 < y;
            }).length;
        };
        const move = (e: PointerEvent) => {
            const d = dragRef.current;
            if (!d || e.pointerId !== d.pointerId) return;
            const lifting = !d.lifted;
            if (lifting) {
                if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < DRAG_THRESHOLD) return;
                d.handle.setPointerCapture?.(d.pointerId);
                releaseRef.current = claimKeyboardForGesture();
            }
            const slot = slotAt(d.id, e.clientY);
            if (lifting || slot !== d.slot) {
                dragRef.current = { ...d, lifted: true, slot };
                setDrag(dragRef.current);
            }
        };
        const up = (e: PointerEvent) => {
            if (dragRef.current && e.pointerId === dragRef.current.pointerId) finish(true);
        };
        const cancel = (e: PointerEvent) => {
            if (dragRef.current && e.pointerId === dragRef.current.pointerId) finish(false);
        };
        const key = (e: KeyboardEvent) => {
            if (e.key !== 'Escape' || !dragRef.current) return;
            e.preventDefault();
            e.stopImmediatePropagation();
            finish(false);
        };
        const lost = () => { if (dragRef.current?.lifted) finish(false); };
        const handle = drag.handle;
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', cancel);
        window.addEventListener('keydown', key, true);
        handle.addEventListener('lostpointercapture', lost);
        return () => {
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', up);
            window.removeEventListener('pointercancel', cancel);
            window.removeEventListener('keydown', key, true);
            handle.removeEventListener('lostpointercapture', lost);
        };
        // The listeners follow the press (its id), not each slot it moves to.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [drag?.id, drag?.pointerId]);

    // An unmount mid-drag gives the keyboard back.
    useEffect(() => () => { releaseRef.current?.(); releaseRef.current = null; }, []);

    const handleFor = (id: string): ReactNode => (readOnly ? undefined : (
        <button
            type="button"
            data-testid="message-drag"
            aria-label="Move message (↑↓)"
            title="Drag to move · ↑↓"
            onPointerDown={(e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                const index = tokensRef.current.tokens.indexOf(id);
                dragRef.current = { id, x: e.clientX, y: e.clientY, lifted: false, slot: index, handle: e.currentTarget, pointerId: e.pointerId };
                setDrag(dragRef.current);
            }}
            onKeyDown={(e) => {
                if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
                e.preventDefault();
                e.stopPropagation();
                const index = tokensRef.current.tokens.indexOf(id);
                reorder(id, e.key === 'ArrowUp' ? index - 1 : index + 1);
            }}
            className="inline-flex items-center justify-center w-5 h-5 rounded-xs text-ink-3 cursor-grab hover:bg-sheet-3 hover:text-ink touch-none"
        >
            <GripVertical className="w-3 h-3" strokeWidth={1.8} />
        </button>
    ));

    const indicator = <div key="drop" data-testid="queue-drop-indicator" aria-hidden className="h-0.5 my-px rounded-full bg-brass" />;
    const lifted = drag?.lifted ? drag : null;
    let others = 0;
    const rows: ReactNode[] = [];
    tokens.forEach((token: QueueToken) => {
        const isDragged = lifted && token === lifted.id;
        if (lifted && !isDragged && others === lifted.slot) rows.push(indicator);
        if (!isDragged) others++;
        if (token === QUEUE_BAR) {
            rows.push(
                <div key="bar" data-testid="queue-bar-line" data-queue-slot="bar" className="flex items-center gap-2 my-1 px-2">
                    <span className="flex-1 border-t border-dashed border-brass" />
                    <span className="text-[0.6875rem] text-ink-3">Held below the bar</span>
                    <span className="flex-1 border-t border-dashed border-brass" />
                </div>,
            );
            return;
        }
        const thread = threads.get(token);
        if (!thread) return;
        rows.push(
            <div key={token} data-queue-slot={token} className={isDragged ? 'opacity-45' : undefined}>
                {render(thread, handleFor(token))}
            </div>,
        );
    });
    if (lifted && others === lifted.slot) rows.push(indicator);

    if (tokens.length === 0) return null;
    return <div ref={rootRef} data-testid="queue-section">{rows}</div>;
}
