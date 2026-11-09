import { Ear, EarOff } from 'lucide-react';

/** Whether a run reads its messages: teal while it listens, ink-3 while it
 *  has not polled them yet; nothing when unknown. */
export function ListeningGlyph({ listening }: { listening: boolean | null }) {
    if (listening === null) return null;
    const Icon = listening ? Ear : EarOff;
    const title = listening ? 'Listening for annotations' : 'Not reading annotations yet';
    return (
        <span data-testid="run-listening" data-listening={listening} title={title} className="inline-flex flex-none">
            <Icon aria-label={title} className={listening ? 'w-3 h-3 text-teal' : 'w-3 h-3 text-ink-3'} strokeWidth={1.8} />
        </span>
    );
}
