import type { ReactNode } from 'react';
import { BookUp, Copy } from 'lucide-react';
import { btn } from '@/components/ui/formStyles';

/**
 * 'This cell' in a Capabilities cell's Manage tab: what is done with the
 * cell as a whole. Copy to channel… puts a copy on another channel's
 * Capabilities; Publish to catalog… writes it into the user catalog for any
 * channel to add. `children` are the lines above the actions (where its
 * packages came from, an update waiting).
 */
export function CellStrip({ onCopy, onPublish, children }: {
    onCopy: () => void;
    onPublish: () => void;
    children?: ReactNode;
}) {
    return <section data-cell-strip="" aria-label="This cell" className="flex flex-col gap-1.5 rounded-md border border-line px-3 py-2">
        {children}
        <div className="flex flex-wrap items-center gap-1.5">
            <button type="button" className={btn} onClick={onCopy}><Copy className="h-3.5 w-3.5" aria-hidden />Copy to channel…</button>
            <button type="button" className={btn} onClick={onPublish}><BookUp className="h-3.5 w-3.5" aria-hidden />Publish to catalog…</button>
        </div>
    </section>;
}
