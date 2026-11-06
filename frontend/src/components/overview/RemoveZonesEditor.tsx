import { useEffect, useId, useMemo, useRef, useState, type RefObject } from 'react';
import { ArrowLeft, ArrowRight, Plus, RotateCcw, X } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { Menu } from '@/components/ui/Menu';
import { menuHead, menuItem, menuSep } from '@/components/ui/menuStyles';
import { fetchChannelTags, type ChannelTag } from '@/lib/api/cells';
import { mergeVocabulary, normalizeTag } from '@/lib/tags';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    REMOVE_ZONES_MAX, addZone, deleteZone, effectiveZones, moveZone, resizeZones, settledZones, unsettledZone, zoneEdges,
    zoneLabel, type RemoveZone, type ZoneAction,
} from './removeZones';

// The remove zones' editor: a dialog off the Removed shelf's config button
// (SlidersHorizontal, right of the search and the tag toggle). A live
// preview of the split whose dividers drag (or step with the arrow keys) to
// resize, one row per zone (move it left or right, its action, the tag it
// adds or takes away with the channel's tags as suggestions, delete), Add
// zone and Reset to one zone. Every change goes to the store at once
// (setRemoveZones), which the bar and the shelf show; useOverviewPersistence
// saves it to the channel. A tag zone still without a tag is not saved,
// and closing the editor drops it (the server would), the other zones
// taking its width.

/** A divider steps this far per arrow key. */
const KEY_STEP = 0.02;

const ACTIONS: [ZoneAction['kind'], string][] = [['remove', 'Standard'], ['add_tag', 'Add tag'], ['remove_tag', 'Remove tag']];

const iconButton = 'inline-flex h-6 w-6 flex-none items-center justify-center rounded-sm text-ink-3 transition-colors hover:bg-sheet-2 hover:text-ink disabled:pointer-events-none disabled:opacity-40';
const field = 'h-6 rounded-sm border border-line bg-sheet px-1.5 text-[0.78125rem] text-ink outline-none focus:border-line-2';

function actionOf(kind: ZoneAction['kind'], previous: ZoneAction): ZoneAction {
    if (kind === 'remove') return { kind };
    return { kind, tag: previous.kind === 'remove' ? '' : previous.tag };
}

/** The split as the bar shows it, with dividers that resize it. */
function Preview({ zones, write }: { zones: RemoveZone[]; write: (next: RemoveZone[]) => void }) {
    const stripRef = useRef<HTMLDivElement>(null);
    const drag = useRef<number | null>(null);
    const edges = zoneEdges(zones);
    const fractionAt = (clientX: number) => {
        const r = stripRef.current?.getBoundingClientRect();
        return r && r.width > 0 ? (clientX - r.left) / r.width : null;
    };
    return (
        <div ref={stripRef} data-zone-preview className="relative mx-1.5 my-1 flex h-8 select-none overflow-hidden rounded-md border border-line-2 bg-canvas">
            {zones.map((zone, i) => (
                <div
                    key={zone.id}
                    data-zone-preview-segment={i}
                    className={cn('flex min-w-0 items-center justify-center px-1.5 text-[0.75rem] font-medium text-ink-2', i > 0 && 'border-l border-dashed border-line-2')}
                    style={{ flex: `0 0 ${(edges[i + 1] - edges[i]) * 100}%` }}
                >
                    <span className="truncate">{zoneLabel(zone.action)}</span>
                </div>
            ))}
            {zones.slice(1).map((zone, k) => (
                <div
                    key={zone.id}
                    role="separator"
                    aria-orientation="vertical"
                    aria-label={`Between zone ${k + 1} and zone ${k + 2}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(edges[k + 1] * 100)}
                    tabIndex={0}
                    data-zone-divider={k}
                    className="group/div absolute inset-y-0 -ml-1 w-2 cursor-col-resize outline-none"
                    style={{ left: `${edges[k + 1] * 100}%`, touchAction: 'none' }}
                    onPointerDown={(e) => {
                        if (e.button !== 0) return;
                        e.preventDefault();
                        drag.current = k;
                        try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* the pointer is gone */ }
                    }}
                    onPointerMove={(e) => {
                        if (drag.current !== k) return;
                        const f = fractionAt(e.clientX);
                        if (f !== null) write(resizeZones(zones, k, f));
                    }}
                    onPointerUp={(e) => {
                        drag.current = null;
                        try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
                    }}
                    onPointerCancel={() => { drag.current = null; }}
                    onKeyDown={(e) => {
                        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
                        e.preventDefault();
                        e.stopPropagation();
                        write(resizeZones(zones, k, edges[k + 1] + (e.key === 'ArrowLeft' ? -KEY_STEP : KEY_STEP)));
                    }}
                >
                    <span className="absolute inset-y-0 left-[0.1875rem] w-0.5 bg-line-2 group-hover/div:bg-ink-3 group-focus-visible/div:bg-teal" />
                </div>
            ))}
        </div>
    );
}

function ZoneRow({ zone, index, count, listId, write }: {
    zone: RemoveZone; index: number; count: number; listId: string;
    /** Apply an edit to the zones as shown. */
    write: (edit: (zones: RemoveZone[]) => RemoveZone[]) => void;
}) {
    const n = index + 1;
    const setAction = (action: ZoneAction) =>
        write(zones => zones.map((z, i) => (i === index ? { ...z, action } : z)));
    return (
        <li data-zone-row={index} className="flex h-[1.875rem] items-center gap-1 px-1.5">
            <button type="button" className={iconButton} aria-label={`Move zone ${n} left`} title="Move left"
                disabled={index === 0} onClick={() => write(zones => moveZone(zones, index, index - 1))}>
                <ArrowLeft className="h-3.5 w-3.5" strokeWidth={1.6} />
            </button>
            <button type="button" className={iconButton} aria-label={`Move zone ${n} right`} title="Move right"
                disabled={index === count - 1} onClick={() => write(zones => moveZone(zones, index, index + 1))}>
                <ArrowRight className="h-3.5 w-3.5" strokeWidth={1.6} />
            </button>
            <select
                aria-label={`Zone ${n} action`}
                className={cn(field, 'w-[6.5rem] flex-none')}
                value={zone.action.kind}
                onChange={(e) => setAction(actionOf(e.target.value as ZoneAction['kind'], zone.action))}
            >
                {ACTIONS.map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
            </select>
            {zone.action.kind !== 'remove' ? (
                <input
                    type="text"
                    list={listId}
                    spellCheck={false}
                    autoComplete="off"
                    aria-label={`Zone ${n} tag`}
                    placeholder="Tag"
                    className={cn(field, 'min-w-0 flex-1 placeholder:text-ink-3')}
                    value={zone.action.tag}
                    onChange={(e) => setAction({ kind: zone.action.kind as 'add_tag' | 'remove_tag', tag: e.target.value })}
                    onBlur={(e) => setAction({ kind: zone.action.kind as 'add_tag' | 'remove_tag', tag: normalizeTag(e.target.value) ?? '' })}
                />
            ) : <span className="flex-1" />}
            <button type="button" className={iconButton} aria-label={`Delete zone ${n}`} title="Delete zone"
                disabled={count === 1} onClick={() => write(zones => deleteZone(zones, index))}>
                <X className="h-3.5 w-3.5" strokeWidth={1.6} />
            </button>
        </li>
    );
}

function EditorBody() {
    const stored = useCollimateStore(s => s.overview.removeZones);
    const setRemoveZones = useCollimateStore(s => s.setRemoveZones);
    const channel = useCollimateStore(s => s.overviewData.channel);
    const liveCells = useCollimateStore(useShallow(s => (s.activeChannel?.main_stacks ?? []).flatMap(stack => stack.cells)));
    const removed = useCollimateStore(s => s.overviewData.removed);
    const [fetched, setFetched] = useState<ChannelTag[]>([]);
    const listId = useId();

    // The channel's tags, the shelf's and the board's, once per opening.
    useEffect(() => {
        if (!channel) return undefined;
        let live = true;
        fetchChannelTags(channel).then(tags => { if (live) setFetched(tags); }).catch(() => { /* the board's and the shelf's tags still suggest */ });
        return () => { live = false; };
    }, [channel]);

    const zones = effectiveZones(stored);
    const vocabulary = useMemo(() => mergeVocabulary(liveCells, removed, fetched), [liveCells, removed, fetched]);
    const write = (next: RemoveZone[]) => setRemoveZones(next);

    return (
        // A portal leaves the DOM but not the React tree: clicks in here
        // must not reach the shelf's handlers.
        <div data-remove-zones-editor onClick={(e) => e.stopPropagation()}>
            <div className={menuHead}>Remove zones</div>
            <p className="px-2.5 pb-1 text-[0.75rem] text-ink-3">
                Drop a card on a zone of the Removed bar or the shelf to remove it with that zone&apos;s action:
                Standard (plain remove), or add or take away a tag.
            </p>
            <Preview zones={zones} write={write} />
            <ul className="list-none py-0.5">
                {zones.map((zone, i) => (
                    <ZoneRow
                        key={zone.id}
                        zone={zone}
                        index={i}
                        count={zones.length}
                        listId={listId}
                        write={edit => write(edit(zones))}
                    />
                ))}
            </ul>
            <datalist id={listId}>
                {vocabulary.map(tag => <option key={tag} value={tag} />)}
            </datalist>
            <div className={menuSep} />
            <button type="button" className={menuItem} disabled={zones.length >= REMOVE_ZONES_MAX} onClick={() => write(addZone(stored))}>
                <Plus className="h-3.5 w-3.5 flex-none" strokeWidth={1.6} />
                Add zone
            </button>
            <button type="button" className={menuItem} disabled={stored.length === 0} onClick={() => write([])}>
                <RotateCcw className="h-3.5 w-3.5 flex-none" strokeWidth={1.6} />
                Reset to one zone
            </button>
        </div>
    );
}

export interface RemoveZonesEditorProps {
    /** The shelf's config button. */
    anchor: RefObject<HTMLElement | null>;
    open: boolean;
    onClose: () => void;
}

export function RemoveZonesEditor({ anchor, open, onClose }: RemoveZonesEditorProps) {
    const close = () => {
        const store = useCollimateStore.getState();
        if (store.overview.removeZones.some(unsettledZone)) store.setRemoveZones(settledZones(store.overview.removeZones));
        onClose();
    };
    return (
        <Menu anchor={anchor} open={open} onClose={close} role="dialog" label="Remove zones" align="start" minWidth={360} maxWidth={420}>
            <EditorBody />
        </Menu>
    );
}
