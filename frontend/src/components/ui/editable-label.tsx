import React, { useState, useEffect, useRef, useLayoutEffect } from 'react';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';

interface EditableLabelProps {
    value: string;
    onSave: (newValue: string) => void;
    className?: string;
    inputClassName?: string;
    placeholder?: string;
    /** Tooltip on the at-rest label (default "Click to edit"). */
    title?: string;
    /** Render a wrapping <textarea> instead of a single-line input, for
     *  prose like descriptions. Enter inserts a newline; blur/Escape behave
     *  as in single-line mode. */
    multiline?: boolean;
    /** Only the focused cell name owns the title shortcut. */
    isActive?: boolean;
}

export const EditableLabel: React.FC<EditableLabelProps> = ({
    value,
    onSave,
    className,
    inputClassName,
    placeholder,
    title,
    multiline = false,
    isActive = false,
}) => {
    const [isEditing, setIsEditing] = useState(false);
    const [localValue, setLocalValue] = useState(value);
    const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

    useEffect(() => {
        if (!isEditing) setLocalValue(value);
    }, [value, isEditing]);

    useLayoutEffect(() => {
        if (isEditing && inputRef.current) {
            inputRef.current.focus();
            inputRef.current.select();
        }
    }, [isEditing]);

    const handleSave = () => {
        if (localValue.trim() !== value) {
            onSave(localValue.trim());
        }
        setIsEditing(false);
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        // In multiline mode Enter inserts a newline (default textarea
        // behavior); single-line mode saves on Enter.
        if (e.key === 'Enter' && !multiline) {
            e.preventDefault(); handleSave();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            setLocalValue(value);
            setIsEditing(false);
        }
    };

    useShortcut(SHORTCUT_IDS.CELL_EDIT_TITLE, event => {
        event.preventDefault(); if (isEditing) handleSave(); else setIsEditing(true);
    }, {enabled:isActive,skipWhenTyping:false});
    const shortcutTitle = useShortcutTooltip(SHORTCUT_IDS.CELL_EDIT_TITLE, 'Click to rename');

    if (isEditing) {
        if (multiline) {
            return (
                <textarea
                    ref={inputRef as React.RefObject<HTMLTextAreaElement>}
                    {...KEYBOARD_OWNER_PROPS}
                    value={localValue}
                    onChange={(e) => setLocalValue(e.target.value)}
                    onBlur={handleSave}
                    onKeyDown={handleKeyDown}
                    placeholder={placeholder}
                    rows={3}
                    className={cn(
                        "w-full resize-y rounded-md border border-line-2 bg-transparent px-1 py-0.5 text-[0.8125rem] text-ink outline-none focus-visible:border-ink-3",
                        inputClassName,
                    )}
                />
            );
        }
        return (
            <Input
                ref={inputRef as React.RefObject<HTMLInputElement>}
                {...KEYBOARD_OWNER_PROPS}
                value={localValue}
                onChange={(e) => setLocalValue(e.target.value)}
                onBlur={handleSave}
                onKeyDown={handleKeyDown}
                placeholder={placeholder}
                className={cn("h-6 py-0 px-1 text-[0.8125rem] font-semibold", inputClassName)}
            />
        );
    }

    return (
        <span
            role="button"
            onClick={() => setIsEditing(true)}
            className={cn(
                "cursor-pointer px-1 rounded-xs transition-colors border border-transparent hover:bg-sheet-2 hover:border-line",
                multiline ? "block whitespace-pre-wrap break-words" : "truncate",
                className
            )}
            title={title ?? (isActive ? shortcutTitle : 'Click to edit')}
        >
            {value || placeholder || ' '}
        </span>
    );
};
