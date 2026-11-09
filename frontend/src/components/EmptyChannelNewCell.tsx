import { Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useShortcutDisplay } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { btnPrimary, kbd } from './ui/formStyles';

// ---------------------------------------------------------------------------
// The empty channel: a quiet line on the canvas and one action, New stack —
// a blank cell in its own stack, the same as the top bar's New stack and
// its shortcut. Choosing a runner for it is the next, separate move.
// ---------------------------------------------------------------------------

export function EmptyChannelNewCell({ isEmpty, isZoomedOut, onNew }: {
    isEmpty: boolean;
    isZoomedOut: boolean;
    onNew: () => void;
}) {
    const shortcut = useShortcutDisplay(SHORTCUT_IDS.STACK_NEW_CELL);
    if (!isEmpty) return null;
    // The overview shows the whole (empty) channel already; New stack stays
    // in the top bar.
    if (isZoomedOut) return null;

    return (
        <div className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 animate-in fade-in duration-300">
            <p className="text-[0.8125rem] text-ink-3 select-none">Nothing on the canvas yet.</p>
            <button
                type="button"
                onClick={onNew}
                className={cn(btnPrimary, 'pointer-events-auto')}
            >
                <Plus className="size-3.5" strokeWidth={1.6} />
                New stack
                {shortcut && <kbd className={cn(kbd, 'ml-1 font-normal')}>{shortcut}</kbd>}
            </button>
        </div>
    );
}
