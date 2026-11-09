import { useEffect, useRef, useState } from 'react';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { cn } from '@/lib/utils';

/** The stack menu's Rename: an inline input in the name's place on a stack
 *  head or a fan bracket. Enter or blur saves (trimmed; empty clears the
 *  name), Esc cancels. It owns its keys (KEYBOARD_OWNER_PROPS) so no scene
 *  shortcut or Esc-cascade rung fires while typing, and its press stays its
 *  own (the head's pan gesture never captures it). */
export function StackRenameInput({ value, onSave, onCancel, className, style }: {
    value: string;
    onSave: (name: string) => void;
    onCancel: () => void;
    /** Placement in the host (the head centres it, the bracket lines it up
     *  with the name). */
    className?: string;
    style?: React.CSSProperties;
}) {
    const [draft, setDraft] = useState(value);
    const inputRef = useRef<HTMLInputElement>(null);
    // Enter / Esc end the edit before the unmount's blur arrives; this
    // keeps that blur from saving a second time (or saving a cancel).
    const doneRef = useRef(false);
    useEffect(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
    }, []);
    const finish = (save: boolean) => {
        if (doneRef.current) return;
        doneRef.current = true;
        if (save) onSave(draft.trim());
        else onCancel();
    };
    return (
        <input
            ref={inputRef}
            {...KEYBOARD_OWNER_PROPS}
            aria-label="Stack name"
            placeholder="Stack name"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => finish(true)}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); finish(true); }
                else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
            }}
            className={cn(
                'h-[1.375rem] min-w-0 rounded-sm border border-line-2 bg-sheet px-2 text-[0.75rem] font-medium text-ink',
                'outline-none placeholder:text-ink-3 focus:border-teal',
                className,
            )}
            // Heads are user-select: none; the name being edited is text the
            // user selects on purpose.
            style={{ userSelect: 'text', ...style }}
        />
    );
}
