import type { Ref } from 'react';
import { Plus, Search } from 'lucide-react';
import type { CapabilityKind } from '@/lib/types';
import { CAPABILITY_KINDS } from '@/lib/capabilityList';
import { btn, btnPrimary, formInput } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** Each kind's chip and group title. */
const KIND_TITLES: Record<CapabilityKind, string> = {
    generator: 'Generators',
    runner: 'Runners',
    renderer: 'Renderers',
    handler: 'Handlers',
};

const CHIP = 'inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-[0.75rem] transition-colors';

/**
 * The manage view's search, kind chips and doors. The search matches names,
 * ids, summaries, file types, a runner's generator and the holding cell,
 * every term; Esc clears it, then blurs it. The chips toggle which groups
 * show (none pressed: all), each with its count under the search.
 */
export function CapabilitiesToolbar({ query, onQuery, kinds, onKinds, counts, onAddFromCatalog, onCreate, searchRef }: {
    query: string;
    onQuery: (query: string) => void;
    kinds: readonly CapabilityKind[];
    onKinds: (kinds: CapabilityKind[]) => void;
    counts: Record<CapabilityKind, number>;
    onAddFromCatalog: () => void;
    onCreate: () => void;
    searchRef?: Ref<HTMLInputElement>;
}) {
    const toggle = (kind: CapabilityKind) => onKinds(kinds.includes(kind) ? kinds.filter(item => item !== kind) : CAPABILITY_KINDS.filter(item => item === kind || kinds.includes(item)));
    return <div data-capabilities-toolbar="" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
            <label className="relative flex min-w-[10rem] flex-1 items-center">
                <Search className="pointer-events-none absolute left-2 size-3.5 text-ink-3" aria-hidden />
                <input ref={searchRef} type="search" value={query} placeholder="Search capabilities" aria-label="Search capabilities"
                    className={cn(formInput, 'w-full pl-7')}
                    onChange={e => onQuery(e.target.value)}
                    onKeyDown={e => {
                        if (e.key !== 'Escape') return;
                        e.preventDefault();
                        if (query) onQuery('');
                        else e.currentTarget.blur();
                    }} />
            </label>
            <button type="button" className={btn} onClick={onAddFromCatalog}>Add from catalog…</button>
            <button type="button" className={btnPrimary} onClick={onCreate}><Plus className="h-3.5 w-3.5" aria-hidden />Create…</button>
        </div>
        <div role="group" aria-label="Show kinds" className="flex flex-wrap gap-1.5">
            {CAPABILITY_KINDS.map(kind => {
                const on = kinds.includes(kind);
                return <button key={kind} type="button" aria-pressed={on} data-kind-chip={kind} onClick={() => toggle(kind)}
                    className={cn(CHIP, on ? 'border-teal bg-teal-soft text-teal' : 'border-line-2 text-ink-2 hover:bg-sheet-2 hover:text-ink')}>
                    {KIND_TITLES[kind]}<span className="font-mono text-[0.6875rem] text-ink-3">{counts[kind]}</span>
                </button>;
            })}
        </div>
    </div>;
}
