import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { panelLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

const KINDS: readonly [string, string][] = [
    ['Generators', 'make files: a program, a terminal or an agent that works in a cell.'],
    ['Runners', 'are saved ways to start a generator: its settings, accounts and prompt. Use Run on any other cell to pick one.'],
    ['Renderers', 'show files: a view for one or more file types.'],
    ['Handlers', 'teach agents a file type and check its files, with checks and skills you review before they are used.'],
];

/** What the Capabilities stack is, at the top of its manage view: open
 *  until the user folds it (component state). */
export function CapabilitiesAbout() {
    const [open, setOpen] = useState(true);
    return <section data-capabilities-concepts="" aria-label="About Capabilities" className="flex flex-col gap-1.5 rounded-md bg-teal-soft px-3 py-2.5">
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}
            className="flex items-center gap-1.5 self-start text-left">
            <ChevronRight className={cn('size-3.5 text-ink-3 transition-transform', open && 'rotate-90')} aria-hidden />
            <span className={panelLabel}>About Capabilities</span>
        </button>
        {open && <div className="flex flex-col gap-1.5 text-[0.78125rem] leading-[1.45] text-ink-2">
            <p className="text-ink">What this channel can make, show and check. Everything on this stack is in use, whichever cell holds it — the cell only keeps it organised. Nothing runs here.</p>
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {KINDS.map(([name, text]) => <li key={name}><span className="font-medium text-ink">{name}</span> {text}</li>)}
            </ul>
            <p className="text-ink-3">Build and Create open a work stack beside this one. Add what they make from that stack’s Files panel. Move a cell off this stack to set its capabilities aside.</p>
        </div>}
    </section>;
}
