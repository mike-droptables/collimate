import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { motion } from 'motion/react';
import { X } from 'lucide-react';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { cn } from '@/lib/utils';
import { btn, btnDanger, btnPrimary, dialog, formInput, iconBtn } from '@/components/ui/formStyles';

// ─── Channel CRUD dialog ──────────────────────────────────────────────────────

interface ChannelDialogProps {
    mode: 'rename' | 'delete';
    activeChannelName: string;
    value: string;
    onChange: (v: string) => void;
    onCancel: () => void;
    onSubmit: () => void;
}

export const ChannelDialog = ({ mode, activeChannelName, value, onChange, onCancel, onSubmit }: ChannelDialogProps) => {
    const inputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        inputRef.current?.focus();
        if (mode === 'rename') inputRef.current?.select();
    }, [mode]);

    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onCancel();
        };
        document.addEventListener('keydown', handleKey);
        return () => document.removeEventListener('keydown', handleKey);
    }, [onCancel]);

    const title = mode === 'rename' ? 'Rename Channel' : 'Delete Channel';

    const submitLabel = mode === 'rename' ? 'Save' : 'Delete';

    const isDelete = mode === 'delete';
    const confirmEnabled = isDelete
        ? value.trim() === activeChannelName
        : value.trim().length > 0 && value.trim() !== activeChannelName;

    // Portal to document.body — AppTopBar has `transform-gpu`, which turns
    // it into the containing block for `position: fixed` descendants. Without
    // the portal, the dialog's `fixed inset-0` maps to the top-bar's own
    // (short) bounds, so the modal appears clipped at the top of the viewport.
    return createPortal(
        <div
            {...KEYBOARD_OWNER_PROPS}
            className="fixed inset-0 z-[300] flex items-center justify-center p-4 bg-(--lift) backdrop-blur-[0.25rem]"
            onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}
        >
            <motion.div
                initial={{ opacity: 0, scale: 0.96, y: 8 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                transition={{ duration: 0.12 }}
                className={cn(dialog, 'w-full max-w-sm')}
            >
                <div className="flex items-center justify-between h-9 px-4 border-b border-line">
                    <h2 className="text-[0.84375rem] font-semibold text-ink">{title}</h2>
                    <button onClick={onCancel} aria-label="Close" className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem]')}>
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="p-4 space-y-3">
                    {isDelete ? (
                        <>
                            <p className="text-[0.78125rem] text-ink-2 leading-relaxed">
                                This will permanently delete <span className="text-ink font-mono">{activeChannelName}</span>,
                                including its full DAG history and any files referenced only by this channel.
                                This cannot be undone.
                            </p>
                            <p className="text-[0.78125rem] text-ink-2">
                                Type <span className="text-red font-mono">{activeChannelName}</span> to confirm:
                            </p>
                        </>
                    ) : (
                        <p className="text-[0.78125rem] text-ink-2">
                            New name
                        </p>
                    )}
                    <input
                        ref={inputRef}
                        type="text"
                        value={value}
                        onChange={(e) => onChange(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter' && confirmEnabled) onSubmit();
                        }}
                        className={formInput}
                        placeholder={isDelete ? activeChannelName : 'channel name'}
                    />
                </div>
                <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
                    <button className={btn} onClick={onCancel}>Cancel</button>
                    <button
                        className={isDelete ? btnDanger : btnPrimary}
                        disabled={!confirmEnabled}
                        onClick={onSubmit}
                    >
                        {submitLabel}
                    </button>
                </div>
            </motion.div>
        </div>,
        document.body,
    );
};

