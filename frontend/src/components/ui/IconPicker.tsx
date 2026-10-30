import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Search, X } from 'lucide-react';
import { ICON_NAMES, ICON_REGISTRY, DEFAULT_GROUP_ICON, resolveIcon } from '@/lib/iconRegistry';
import { cn } from '@/lib/utils';
import { menuSurface } from '@/components/ui/menuStyles';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';

interface IconPickerProps {
    /** Currently-selected icon key (null/undefined = none). */
    value?: string | null;
    /** Called with the chosen key, or null when the user clears it. */
    onChange: (next: string | null) => void;
    /** Tooltip on the trigger button. */
    title?: string;
    className?: string;
}

/** A small trigger button showing the current icon; clicking opens a
 *  searchable popover grid over ICON_REGISTRY. Used in the generator-source
 *  cell header to pick the cell's "group icon". */
export function IconPicker({ value, onChange, title, className }: IconPickerProps) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState('');
    const triggerRef = useRef<HTMLButtonElement | null>(null);
    const popoverRef = useRef<HTMLDivElement | null>(null);
    const searchRef = useRef<HTMLInputElement | null>(null);
    const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null);

    const triggerIcon = resolveIcon(value) ?? DEFAULT_GROUP_ICON;

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return ICON_NAMES;
        return ICON_NAMES.filter(n => n.includes(q));
    }, [query]);

    // Position the portalled popover under the trigger; recompute on open.
    useEffect(() => {
        if (!open) return;
        const r = triggerRef.current?.getBoundingClientRect();
        if (r) setAnchor({ top: r.bottom + 4, left: r.left });
        requestAnimationFrame(() => { try { searchRef.current?.focus(); } catch { /* detached */ } });
    }, [open]);

    // Close on outside click / Escape.
    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            const t = e.target as Node | null;
            if (!t) return;
            if (triggerRef.current?.contains(t) || popoverRef.current?.contains(t)) return;
            setOpen(false);
        };
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); } };
        document.addEventListener('mousedown', onDown, true);
        document.addEventListener('keydown', onKey, true);
        return () => {
            document.removeEventListener('mousedown', onDown, true);
            document.removeEventListener('keydown', onKey, true);
        };
    }, [open]);

    const choose = (name: string | null) => {
        onChange(name);
        setOpen(false);
        setQuery('');
    };

    return (
        <>
            <button
                ref={triggerRef}
                type="button"
                title={title ?? 'Pick an icon'}
                aria-label={title ?? 'Pick an icon'}
                onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
                className={cn(
                    'flex items-center justify-center rounded-xs border transition-colors flex-none w-6 h-6',
                    value
                        ? 'border-line-2 text-ink hover:bg-sheet-2'
                        : 'border-line text-ink-3 hover:text-ink hover:bg-sheet-2',
                    className,
                )}
            >
                {React.createElement(triggerIcon, { className: 'w-3.5 h-3.5' })}
            </button>
            {open && anchor && createPortal(
                <div
                    ref={popoverRef}
                    {...KEYBOARD_OWNER_PROPS}
                    onClick={(e) => e.stopPropagation()}
                    onMouseDown={(e) => e.stopPropagation()}
                    style={{ position: 'fixed', top: anchor.top, left: anchor.left, zIndex: 400 }}
                    className={cn(menuSurface, 'w-64 p-0 flex flex-col overflow-hidden')}
                >
                    <div className="flex items-center gap-2 h-[1.875rem] px-2.5 border-b border-line">
                        <Search className="w-3.5 h-3.5 text-ink-3 flex-none" />
                        <input
                            ref={searchRef}
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                            placeholder="Search icons…"
                            className="flex-1 min-w-0 bg-transparent text-[0.8125rem] text-ink placeholder:text-ink-3 outline-none"
                        />
                        {value && (
                            <button
                                type="button"
                                onClick={() => choose(null)}
                                title="Clear icon"
                                className="text-ink-3 hover:text-red flex-none"
                            >
                                <X className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                    <div className="grid grid-cols-7 gap-1 p-2 overflow-y-auto max-h-56">
                        {filtered.length === 0 && (
                            <div className="col-span-7 px-2 py-4 text-center text-[0.75rem] text-ink-3 italic">
                                No icons match.
                            </div>
                        )}
                        {filtered.map(name => {
                            const Icon = ICON_REGISTRY[name];
                            const active = name === value;
                            return (
                                <button
                                    key={name}
                                    type="button"
                                    title={name}
                                    onClick={() => choose(name)}
                                    className={cn(
                                        'flex items-center justify-center w-7 h-7 rounded-sm transition-colors',
                                        active
                                            ? 'bg-brass-soft text-brass ring-1 ring-brass'
                                            : 'text-ink-2 hover:bg-sheet-2 hover:text-ink',
                                    )}
                                >
                                    <Icon className="w-4 h-4" />
                                </button>
                            );
                        })}
                    </div>
                </div>,
                document.body,
            )}
        </>
    );
}
