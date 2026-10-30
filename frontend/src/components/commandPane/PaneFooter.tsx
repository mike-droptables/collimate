// The Commands pane's footer (the mock's `.cmdk`): the 36px row with the
// view's kbd hints and the Esc button, used alike by the commands view and
// the settings view.

import { kbd } from '@/components/ui/formStyles';

export interface PaneFooterProps {
    /** The key hints, each `[keys, what they do]`. */
    hints: readonly (readonly [string, string])[];
    /** What Esc does here ("to cell", "back"): the button's words. */
    escLabel: string;
    onEsc: () => void;
}

/** The pane's footer: the view's key hints, then the Esc button. */
export function PaneFooter({ hints, escLabel, onEsc }: PaneFooterProps) {
    return (
        <div className="flex-none h-9 flex items-center gap-3 px-3.5 border-t border-line text-[0.71875rem] text-ink-3 whitespace-nowrap">
            {hints.map(([keys, label]) => (
                <span key={label} className="inline-flex items-center gap-[0.3125rem]"><kbd className={kbd}>{keys}</kbd> {label}</span>
            ))}
            <button
                type="button"
                onClick={onEsc}
                className="ml-auto -mr-1 inline-flex items-center gap-[0.3125rem] rounded-xs px-1 py-0.5 text-ink-3 hover:text-ink hover:bg-sheet-2"
            >
                <kbd className={kbd}>esc</kbd> {escLabel}
            </button>
        </div>
    );
}
