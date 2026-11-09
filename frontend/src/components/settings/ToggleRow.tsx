import React from 'react';
import { cn } from '@/lib/utils';

/** The mock's toggle: ink when on, line-2 when off, a sheet knob. It has no
 *  visible text of its own, so `label` is its accessible name. */
export function Switch({ checked, label, disabled, onChange }: {
    checked: boolean;
    label: string;
    disabled?: boolean;
    onChange: (next: boolean) => void;
}) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            aria-label={label}
            disabled={disabled}
            onClick={() => onChange(!checked)}
            className={cn(
                'relative inline-flex h-[1.125rem] w-[1.875rem] flex-none items-center rounded-full transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed',
                checked ? 'bg-ink' : 'bg-line-2',
            )}
        >
            <span className={cn(
                'inline-block h-3.5 w-3.5 rounded-full bg-sheet transition-transform',
                checked ? 'translate-x-[0.875rem]' : 'translate-x-[0.125rem]',
            )} />
        </button>
    );
}

/** A settings row with one switch: a hairline card holding the icon, the
 *  label and its description, and the `Switch`, which is labelled by
 *  `label`. */
export function ToggleRow({ icon, label, desc, checked, disabled, onChange }: {
    icon: React.ReactNode;
    label: string;
    desc: string;
    checked: boolean;
    disabled?: boolean;
    onChange: (next: boolean) => void;
}) {
    return (
        <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-md border border-line">
            <div className="flex items-center gap-2.5 min-w-0">
                <span className="flex-none text-ink-3">{icon}</span>
                <div className="min-w-0">
                    <div className="text-[0.8125rem] text-ink">{label}</div>
                    <div className="text-[0.75rem] text-ink-2">{desc}</div>
                </div>
            </div>
            <Switch checked={checked} label={label} disabled={disabled} onChange={onChange} />
        </div>
    );
}
