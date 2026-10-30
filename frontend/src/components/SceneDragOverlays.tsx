import type { CSSProperties, ComponentProps } from 'react';
import { motion, AnimatePresence, type MotionValue } from 'motion/react';
import { ArrowDown, X, Square, Circle, Layers } from 'lucide-react';
import type { useStackDrag } from '@/hooks/useStackDrag';
import type { useStackCamera } from '@/hooks/useStackCamera';
import { columnCardRect } from '@/hooks/useStackLayout';
import type { Stack } from '@/lib/types';
import { StackGroup, NO_OP_STACK_CALLBACKS } from './StackGroup';
import { CardChromeOverlays } from './CardChromeOverlays';
import { cn } from '@/lib/utils';

type Drag = ReturnType<typeof useStackDrag>;
type Chrome = ComponentProps<typeof CardChromeOverlays>;
interface WorldOverlayProps extends Pick<Drag, 'previewCellData' | 'insertLineX' | 'dragGhost' | 'items' | 'arrowState'> {
    stackTopOffset: number;
    panAlignSnap: ReturnType<typeof useStackCamera>['panAlignSnap'];
    cardFrame: Chrome['frame'];
    visibleTopIndices: Chrome['visibleTopIndices'];
    gap: Chrome['gap'];
    screenMax: Chrome['screenMax'];
    zoomMV: Chrome['zoom'];
    edgeResizeActive: boolean;
    cellDiffZonesActive: boolean;
    onSelectStack: Chrome['onSelectColumn'];
}

/** The pending-move badge: a disc over a tag naming the move. A move is a
 *  live state (teal); cancelling it is destructive (red). */
const arrowDisc = 'p-4 rounded-full border-4 shadow-(--shadow)';
const arrowTag = 'mt-2 bg-sheet px-3 h-[1.625rem] inline-flex items-center rounded-sm border text-[0.75rem] font-semibold uppercase tracking-[0.08em] shadow-(--shadow)';

/** World-space indicators stay inside the scene camera transform. */
export function SceneDragOverlays({ previewCellData, insertLineX, dragGhost, items, arrowState,
    stackTopOffset, panAlignSnap, cardFrame, visibleTopIndices, gap, screenMax, zoomMV,
    edgeResizeActive, cellDiffZonesActive, onSelectStack }: WorldOverlayProps) {
    return <>
                    {/* Transparent slot preview at drop target — marks the
                        landing slot while the real cell follows the cursor
                        in screen space (see floatingCellData below). */}
                    <AnimatePresence>
                        {previewCellData && (
                            <motion.div
                                key="cell-slot-preview"
                                initial={{ opacity: 0, x: previewCellData.x }}
                                animate={{ opacity: 1, x: previewCellData.x }}
                                exit={{ opacity: 0 }}
                                transition={{
                                    x: { type: 'spring', stiffness: 500, damping: 38, mass: 0.7 },
                                    opacity: { duration: 0.15 },
                                }}
                                style={{
                                    position: 'absolute',
                                    left: 0,
                                    top: previewCellData.y,
                                    width: previewCellData.width,
                                    height: previewCellData.height,
                                    pointerEvents: 'none',
                                    zIndex: 900,
                                    borderRadius: 8,
                                    border: '2px dashed var(--teal)',
                                    background: 'var(--teal-soft)',
                                    boxSizing: 'border-box',
                                }}
                            />
                        )}
                    </AnimatePresence>

                    {/* Stack drag insert line */}
                    {insertLineX !== null && dragGhost?.type === 'stack' && (
                        <div style={{
                            position: 'absolute',
                            left: insertLineX,
                            top: stackTopOffset,
                            bottom: 0,
                            width: 3,
                            background: 'var(--teal)',
                            boxShadow: '0 0 0 3px var(--teal-soft)',
                            transform: 'translateX(-50%)',
                            zIndex: 1000,
                            pointerEvents: 'none',
                            borderRadius: 2,
                        }} />
                    )}

                    {/* Pan-align glow — a center-pan has parked the camera
                        near a snap target for the panned stack: light the
                        matching cell edge (left/right align) or the cell's
                        top edge (center). Rendered in world space AFTER the
                        surface overlay (z 110) so it paints above the pooled
                        surfaces; world coordinates keep it glued to the cell
                        while the camera moves. Release snaps the camera to
                        the exact alignment (useStackCamera). */}
                    {panAlignSnap && (() => {
                        const item = items.find(it => it.id === panAlignSnap.stackId);
                        if (!item || item.computedWidth <= 0) return null;
                        const card = columnCardRect(item, cardFrame);
                        const { left, top } = card;
                        const edge: CSSProperties = {
                            position: 'absolute',
                            zIndex: 120,
                            pointerEvents: 'none',
                            background: 'var(--teal)',
                            boxShadow: '0 0 0 3px var(--teal-soft)',
                            borderRadius: 2,
                        };
                        return (
                            <motion.div
                                key={`pan-align-${panAlignSnap.mode}`}
                                data-pan-align={panAlignSnap.mode}
                                initial={{ opacity: 0 }}
                                animate={{ opacity: 0.85 }}
                                transition={{ duration: 0.12 }}
                                style={panAlignSnap.mode === 'center'
                                    ? { ...edge, left, top, width: item.computedWidth, height: 3 }
                                    : {
                                        ...edge,
                                        top,
                                        height: card.bottom - top,
                                        width: 3,
                                        left: panAlignSnap.mode === 'left'
                                            ? left
                                            : left + item.computedWidth - 3,
                                    }}
                            />
                        );
                    })()}

                    <CardChromeOverlays
                        columns={items}
                        visibleTopIndices={visibleTopIndices}
                        frame={cardFrame}
                        gap={gap}
                        screenMax={screenMax}
                        zoom={zoomMV}
                        edgeResizeActive={edgeResizeActive}
                        diffZonesActive={cellDiffZonesActive}
                        onSelectColumn={onSelectStack}
                    />

                    {/* Pending move arrow indicator */}
                    {arrowState && (
                        <div style={{
                            position: 'absolute',
                            left: arrowState.x,
                            top: '50%',
                            transform: 'translate(-50%, -50%)',
                            zIndex: 1000,
                            pointerEvents: 'none',
                        }}>
                            <div className="flex flex-col items-center">
                                {arrowState.isCancel ? (
                                    <>
                                        <div className={cn(arrowDisc, 'bg-red text-sheet border-sheet')}>
                                            <X className="w-10 h-10 stroke-[3]" />
                                        </div>
                                        <div className={cn(arrowTag, 'border-red text-red')}>
                                            CANCEL
                                        </div>
                                    </>
                                ) : arrowState.mode === 'stack' ? (
                                    <>
                                        <div className={cn(arrowDisc, 'bg-teal text-sheet border-sheet')}>
                                            <Square className="w-10 h-10 stroke-[3]" />
                                        </div>
                                        <div className={cn(arrowTag, 'border-teal text-ink')}>
                                            {arrowState.type === 'stack' ? (
                                                <div className="flex items-center gap-1.5">
                                                    <Layers className="w-4 h-4 stroke-[2.5]" />
                                                    <span>MERGE STACK</span>
                                                </div>
                                            ) : (
                                                <div className="flex items-center gap-1.5">
                                                    <Circle className="w-4 h-4 stroke-[2.5]" />
                                                    <span>MERGE CELL</span>
                                                </div>
                                            )}
                                        </div>
                                    </>
                                ) : (
                                    <>
                                        <div className={cn(arrowDisc, 'bg-sheet text-teal border-teal')}>
                                            <ArrowDown className="w-8 h-8 stroke-[3]" />
                                        </div>
                                        <div className={cn(arrowTag, 'border-teal text-ink')}>
                                            {arrowState.type === 'stack' ? (
                                                <div className="flex items-center gap-1.5">
                                                    <Layers className="w-4 h-4 stroke-[2.5]" />
                                                    <span>INSERT STACK</span>
                                                </div>
                                            ) : (
                                                <div className="flex items-center gap-1.5">
                                                    <Circle className="w-4 h-4 stroke-[2.5]" />
                                                    <span>INSERT CELL</span>
                                                </div>
                                            )}
                                        </div>
                                    </>
                                )}
                            </div>
                        </div>
                    )}

    </>;
}

interface CursorOverlayProps extends Pick<Drag, 'floatingCellData' | 'dragGhost'> {
    floatingPreviewStack: Stack | null;
    floatingX: MotionValue<number>;
    floatingY: MotionValue<number>;
    ghostTagX: MotionValue<number>;
    ghostTagY: MotionValue<number>;
}

/** Cursor previews stay outside the camera transform in fixed screen pixels. */
export function SceneCursorOverlays({ floatingCellData, dragGhost, floatingPreviewStack,
    floatingX, floatingY, ghostTagX, ghostTagY }: CursorOverlayProps) {
    return <>
            {/* Cell drag — the actual cell follows the cursor in screen space,
                anchored at the exact grab point so the pointer stays in the
                same spot on the card. The outer box is in screen pixels (matches
                what the user saw at drag start); the inner StackGroup is rendered
                at *natural* (pre-zoom) size with a matching scale(zoom) transform
                so its internal layout — font sizes, padding, icon sizes — stays
                identical to its appearance inside the zoomed canvas. Without the
                scale, zoomed-out drags show full-size text inside a shrunken box. */}
            {floatingCellData && floatingPreviewStack && (() => {
                // Thumbnail mode (main-view grab handle): paint a small
                // square card centered on the cursor. Natural-size preview
                // would dwarf the canvas and the inner overview card would
                // leave the tall main-view rect empty below it.
                const isThumb = floatingCellData.previewMode === 'thumbnail';
                // Moderate, substantial tile — the prior 200px read as a tiny
                // chip floating away from the stack.
                const THUMB_PX = 280;
                const outerW = isThumb ? THUMB_PX : floatingCellData.width;
                const outerH = isThumb ? THUMB_PX : floatingCellData.height;
                // Center the thumbnail on the cursor so it tracks directly under
                // the pointer. (The earlier math centered on the card's midpoint,
                // which—because the grab handle sits at the card's top—pushed the
                // preview well below the mouse.)
                const innerNaturalSide = isThumb
                    ? THUMB_PX / (floatingCellData.dragZoom || 1)
                    : floatingCellData.naturalWidth;
                const innerNaturalH = isThumb ? innerNaturalSide : floatingCellData.naturalHeight;
                return (
                    <motion.div
                        style={{
                            position: 'fixed',
                            left: 0,
                            top: 0,
                            x: floatingX,
                            y: floatingY,
                            width: outerW,
                            height: outerH,
                            zIndex: 9998,
                            pointerEvents: 'none',
                            borderRadius: 8,
                            overflow: 'hidden',
                            boxShadow: 'var(--shadow)',
                            opacity: 0.95,
                        }}
                    >
                        <div
                            style={{
                                width: innerNaturalSide,
                                height: innerNaturalH,
                                transform: `scale(${floatingCellData.dragZoom})`,
                                transformOrigin: 'top left',
                            }}
                        >
                            <StackGroup
                                stack={floatingPreviewStack}
                                width={innerNaturalSide}
                                height={innerNaturalH}
                                isSelected={false}
                                viewMode="overview"
                                callbacks={NO_OP_STACK_CALLBACKS}
                            />
                        </div>
                    </motion.div>
                );
            })()}

            {/* Drag ghost — fixed to cursor in screen space */}
            {dragGhost && dragGhost.type === 'stack' && (
                <motion.div style={{
                    position: 'fixed',
                    left: 0, top: 0, x: ghostTagX, y: ghostTagY,
                    zIndex: 9999,
                    pointerEvents: 'none',
                    background: 'var(--sheet)',
                    border: '1px solid var(--teal)',
                    borderRadius: 'calc(var(--radius) - 2px)',
                    padding: '0.25rem 0.625rem',
                    fontSize: '0.75rem',
                    color: 'var(--teal)',
                    boxShadow: 'var(--shadow)',
                    whiteSpace: 'nowrap',
                }}>
                    Moving column
                </motion.div>
            )}
    </>;
}
