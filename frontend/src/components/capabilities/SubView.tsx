import type { ReactNode } from 'react';
import { ArrowLeft } from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { btn, panelLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { escapeGoesBack } from './manageActions';

/** A view that replaces the manage list (an editor, Create, the catalog):
 *  'Back to Capabilities' over its title, then its body. Esc goes back. */
export function SubView({ title, onBack, children, scroll = true }: { title: string; onBack: () => void; children: ReactNode; scroll?: boolean }) {
    return <div data-capabilities-subview="" className="flex min-h-0 flex-1 flex-col" onKeyDown={e => escapeGoesBack(e, onBack)}>
        <div className="flex h-10 flex-none items-center gap-2 border-b border-line px-3.5">
            <button type="button" className={cn(btn, 'border-transparent px-1.5')} onClick={onBack}>
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                <span className={panelLabel}>Back to Capabilities</span>
            </button>
            <span className="min-w-0 flex-1 truncate text-[0.8125rem] text-ink">{title}</span>
        </div>
        {scroll
            ? <ScrollArea className="min-h-0 flex-1"><div className="px-3.5 pt-3 pb-5 text-[0.8125rem]">{children}</div></ScrollArea>
            : <div className="flex min-h-0 flex-1 flex-col text-[0.8125rem]">{children}</div>}
    </div>;
}
