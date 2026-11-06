import type { ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { btn, panelLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** Why a catalog failed to load, with Retry: inside a role=alert. */
export function LoadFailure({ what, error, onRetry }: { what: string; error: string; onRetry: () => void }) {
    return <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md bg-red-soft px-2.5 py-1.5 text-[0.75rem] text-red">
        <span className="min-w-0 flex-1">Couldn’t load {what}: {error}</span>
        <button type="button" className={btn} onClick={onRetry}>Retry</button>
    </div>;
}

/** The collapsed 'Built-in (n)' disclosure under a group's own entries. */
export function BuiltinDisclosure({ count, open, onToggle, children }: { count: number; open: boolean; onToggle: () => void; children: ReactNode }) {
    if (!count) return null;
    return <div data-builtin-disclosure="" className="flex flex-col gap-2">
        <button type="button" aria-expanded={open} onClick={onToggle}
            className="flex items-center gap-1.5 self-start rounded-xs px-0.5 text-[0.75rem] text-ink-2 hover:text-ink">
            <ChevronRight className={cn('size-3.5 text-ink-3 transition-transform', open && 'rotate-90')} aria-hidden />
            Built-in ({count})
        </button>
        {open && children}
    </div>;
}

/**
 * One group of the manage view's list (Generators, Runners, Renderers,
 * Handlers): '<Type> · <count>' ('n of m' while a search narrows it), its
 * one-line subtitle and an optional header action, then its body: a load
 * failure, 'Loading…', the empty text, or the entries.
 */
export function CapabilityGroup({ kind, title, subtitle, shown, total, filtering, action, loading, error, onRetry, empty, children }: {
    kind: string;
    title: string;
    subtitle: string;
    shown: number;
    total: number;
    filtering: boolean;
    action?: ReactNode;
    loading: boolean;
    error: string;
    onRetry: () => void;
    /** Said when the group holds nothing (never while a search narrows it). */
    empty: ReactNode;
    children: ReactNode;
}) {
    const headingId = `capabilities-group-${kind}`;
    return <section aria-labelledby={headingId} data-capability-group={kind} className="flex flex-col gap-2">
        <div className="flex items-end gap-2 border-b border-line pb-1.5">
            <div className="flex min-w-0 flex-1 flex-col">
                <h4 id={headingId} className={cn(panelLabel, 'mx-0.5')}>{title} · {filtering ? `${shown} of ${total}` : total}</h4>
                <p className="mx-0.5 text-[0.75rem] text-ink-3">{subtitle}</p>
            </div>
            {action}
        </div>
        {error && <LoadFailure what={title.toLowerCase()} error={error} onRetry={onRetry} />}
        {!error && loading && <p className="px-0.5 text-[0.75rem] text-ink-3">Loading {title.toLowerCase()}…</p>}
        {!loading && !filtering && total === 0 && <div className="px-0.5 text-[0.78125rem] text-ink-3">{empty}</div>}
        {children}
    </section>;
}
