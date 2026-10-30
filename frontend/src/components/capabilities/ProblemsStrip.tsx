import { useState } from 'react';
import { AlertTriangle, ChevronRight } from 'lucide-react';
import type { CapabilityProblem } from '@/lib/api/capabilitiesStack';
import { btn } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** 'Needs attention': every file on Capabilities that is not a working
 *  capability, folded to one line until opened; each row opens its cell.
 *  Nothing shows while there are none. */
export function ProblemsStrip({ problems, cellName, onShowCell }: {
    problems: readonly CapabilityProblem[];
    cellName: (cellId: string) => string;
    onShowCell: (cellId: string) => void;
}) {
    const [open, setOpen] = useState(false);
    if (!problems.length) return null;
    const line = problems.length === 1 ? '1 file here isn’t a working capability' : `${problems.length} files here aren’t working capabilities`;
    return <section data-capability-problems="" aria-label="Needs attention" className="flex flex-col gap-1.5 rounded-md border border-brass px-3 py-2">
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)} className="flex items-center gap-1.5 self-start text-left text-[0.78125rem] text-ink">
            <ChevronRight className={cn('size-3.5 text-ink-3 transition-transform', open && 'rotate-90')} aria-hidden />
            <AlertTriangle className="size-3.5 text-brass" aria-hidden />
            {line}
        </button>
        {open && <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {problems.map(problem => <li key={`${problem.cell_id}:${problem.path}`} data-problem={problem.path} className="flex items-start gap-2">
                <span className="min-w-0 flex-1 text-[0.75rem] leading-[1.45] text-ink-2">
                    <span className="break-all font-mono text-ink">{problem.path}</span> in {cellName(problem.cell_id) || 'a cell'}: {problem.error}
                </span>
                <button type="button" className={btn} onClick={() => onShowCell(problem.cell_id)}>Open cell</button>
            </li>)}
        </ul>}
    </section>;
}
