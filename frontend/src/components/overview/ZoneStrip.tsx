import type { CSSProperties } from 'react';
import { cn } from '@/lib/utils';
import { effectiveZones, zoneEdges, zoneLabel, type RemoveZone } from './removeZones';

// The remove zones laid over the Removed bar and the shelf's drop area: one
// overlay for both, so the two show the same split and the drag
// (useStackDrag) resolves the zone under the pointer against this strip's
// rect (`data-rm-zone-strip`). At rest it is the thin dividers between two
// or more zones; while a removable board card is held (`labelled`) each zone
// is a segment with its label, and the zone under the card (`lit`) is lit
// teal.

export interface ZoneStripProps {
    zones: readonly RemoveZone[];
    /** The segments with their labels (a removable board card is held). */
    labelled: boolean;
    /** The segment the held card would be removed to, else null. */
    lit: number | null;
    /** The shelf's drop area: a teal frame round the lit segment and taller
     *  labels; the bar's is a band. */
    variant: 'bar' | 'shelf';
    className?: string;
    style?: CSSProperties;
}

export function ZoneStrip({ zones, labelled, lit, variant, className, style }: ZoneStripProps) {
    const list = effectiveZones(zones);
    const edges = zoneEdges(list);
    return (
        <div
            aria-hidden
            data-rm-zone-strip
            data-labelled={labelled || undefined}
            className={cn('pointer-events-none absolute inset-y-0 flex', className)}
            style={style}
        >
            {labelled ? list.map((zone, i) => {
                const on = lit === i;
                return (
                    <div
                        key={zone.id}
                        data-rm-zone={i}
                        data-lit={on || undefined}
                        className={cn(
                            'relative flex min-w-0 items-center justify-center overflow-hidden px-2',
                            i > 0 && 'border-l border-dashed border-line-2',
                            variant === 'bar' ? 'bg-canvas' : 'bg-canvas/55',
                            on ? 'text-teal' : 'text-ink-2',
                            on && variant === 'shelf' && 'border-2 border-solid border-teal',
                        )}
                        style={{ flex: `0 0 ${(edges[i + 1] - edges[i]) * 100}%` }}
                    >
                        {/* The soft teal is translucent: it lies on the segment's own ground. */}
                        {on && <span className="absolute inset-0 bg-teal-soft" />}
                        <span className={cn('relative truncate font-medium', variant === 'bar' ? 'text-[0.75rem]' : 'text-[0.8125rem]')}>
                            {zoneLabel(zone.action)}
                        </span>
                    </div>
                );
            }) : list.length > 1 && list.slice(1).map((zone, i) => (
                <span
                    key={zone.id}
                    data-rm-zone-divider={i + 1}
                    className="absolute top-[0.5rem] bottom-[0.5rem] w-px bg-line-2"
                    style={{ left: `${edges[i + 1] * 100}%` }}
                />
            ))}
        </div>
    );
}
