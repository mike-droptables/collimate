import type { ReactNode } from 'react';
import type { PackageRow } from '@/lib/api/capabilityCells';
import { chip, sectionLabel } from '@/components/ui/formStyles';

/** A summary value as one line of text (a list as its entries, a mapping
 *  as 'key: value' pairs). */
function summaryText(value: unknown): string {
    if (Array.isArray(value)) return value.length ? value.map(summaryText).join(', ') : 'none';
    if (value !== null && typeof value === 'object') {
        if (!Object.keys(value).length) return 'none';
        return Object.entries(value).map(([key, item]) => `${key}: ${summaryText(item)}`).join('; ');
    }
    return String(value);
}

/** A labelled group of a cell-wide review (Copy to channel, Publish to
 *  catalog, Review and replace). */
export function ReviewSection({ label, children }: { label: string; children: ReactNode }) {
    return <section role="group" aria-label={label} className="flex flex-col gap-1.5">
        <h4 className={sectionLabel}>{label}</h4>
        {children}
    </section>;
}

/** The packages of one cell as a review lists them: kind, name and id,
 *  then every manifest key Add to's review shows (the privilege keys
 *  among them). */
export function PackageList({ packages }: { packages: readonly PackageRow[] }) {
    if (!packages.length) return <p className="text-[0.75rem] text-ink-3">No packages.</p>;
    return <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {packages.map(row => {
            const facts = Object.entries(row.summary).filter(([key]) => key !== 'name');
            return <li key={row.folder} data-review-package={row.id} className="flex flex-col gap-0.5 border-t border-line pt-1.5 first:border-t-0 first:pt-0">
                <span className="flex flex-wrap items-center gap-1.5">
                    <span className={chip}>{row.kind}</span>
                    <span className="font-medium text-ink">{row.name}</span>
                    <span className="font-mono text-[0.71875rem] text-ink-3">{row.id}{row.version ? ` · ${row.version}` : ''}</span>
                </span>
                {facts.length > 0 && <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[0.75rem]">
                    {facts.map(([key, value]) => <div key={key} className="contents">
                        <dt className="text-ink-3">{key}</dt>
                        <dd className="min-w-0 break-words text-ink-2">{summaryText(value)}</dd>
                    </div>)}
                </dl>}
            </li>;
        })}
    </ul>;
}
