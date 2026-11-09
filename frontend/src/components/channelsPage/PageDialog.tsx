import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { cn } from '@/lib/utils';
import { dialog, iconBtn } from '@/components/ui/formStyles';

interface PageDialogProps {
    /** The dialog's accessible name. */
    label: string;
    /** The heading in its 36px head. */
    title: string;
    /** Closes on Escape, the X and a click on the backdrop, unless `busy`. */
    onClose: () => void;
    busy?: boolean;
    /** The footer's buttons, right-aligned. */
    footer: ReactNode;
    children: ReactNode;
    /** Wider for a list (a cleanup preview). */
    wide?: boolean;
}

/**
 * The channels page's dialog plate (the shape DeleteChannelDialog has): a
 * portal over the page at `z-[460]`, a head with the title and Close, a
 * body and a footer of buttons. The Locations and Storage tabs' dialogs
 * (add a location, claim, move, the cleanups, uv's cache) are built on it.
 */
export function PageDialog({ label, title, onClose, busy = false, footer, children, wide = false }: PageDialogProps) {
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onClose(); };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [busy, onClose]);

    return createPortal(
        <div {...KEYBOARD_OWNER_PROPS}
            className="fixed inset-0 z-[460] flex items-center justify-center p-4 bg-(--lift) backdrop-blur-[0.25rem]"
            onClick={e => { if (e.target === e.currentTarget && !busy) onClose(); }}>
            <div role="dialog" aria-label={label} className={cn(dialog, 'flex max-h-[85vh] w-full flex-col', wide ? 'max-w-xl' : 'max-w-md')}>
                <div className="flex h-9 flex-none items-center justify-between border-b border-line px-4">
                    <h2 className="text-[0.84375rem] font-semibold text-ink">{title}</h2>
                    <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem]')}>
                        <X className="h-4 w-4" />
                    </button>
                </div>
                <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4 text-[0.78125rem] leading-relaxed text-ink-2 [overflow-wrap:anywhere]">{children}</div>
                <div className="flex flex-none items-center justify-end gap-2 border-t border-line px-4 py-3">{footer}</div>
            </div>
        </div>,
        document.body,
    );
}
