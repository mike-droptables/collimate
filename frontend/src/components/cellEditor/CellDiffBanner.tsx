import { GitCompareArrows } from 'lucide-react';
import { cn } from '@/lib/utils';
import { btn } from '@/components/ui/formStyles';

/** The band a cell in a cell-pair diff carries directly under its header:
 *  what it is diffed against, and the way out. The diff is an on-state of
 *  the cell, so the band is teal (live fill, teal ink). */
export function CellDiffBanner({ baseName, loading, onExit }: {
    baseName: string;
    /** File operations use pinned bodies and conditional durable receipts. */
    loading: boolean;
    onExit: () => void;
}) {
    const name = baseName || 'untitled cell';
    return (
        <div
            data-cell-diff-banner
            className="flex items-center gap-2 px-3 h-[1.75rem] flex-shrink-0 border-b border-line bg-teal-soft"
            onPointerDown={(e) => e.stopPropagation()}
        >
            <GitCompareArrows className="w-3.5 h-3.5 text-teal flex-shrink-0" aria-hidden />
            <span className="flex-1 min-w-0 truncate text-[0.75rem] text-ink" title={`Diff against ${name}`}>
                Diff against <span className="font-semibold">{name}</span>
                {loading && <span className="text-ink-3"> · loading…</span>}
            </span>
            <button
                type="button"
                onClick={onExit}
                aria-label="Exit diff"
                title="Exit diff"
                className={cn(btn, 'h-[1.375rem] px-2 text-[0.75rem] flex-shrink-0')}
            >
                Exit
            </button>
        </div>
    );
}
