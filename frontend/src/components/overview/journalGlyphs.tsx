// The journal's glyphs, shared by the pane and the journal-style card: the
// run ring, the pip, the move glyphs and a file name.

import type { ReactNode } from 'react';
import type { JournalKind } from '@/lib/api/cells';
import { cn } from '@/lib/utils';
import { baseName, fileLabels, type RunMark } from './journalText';

/** The run ring: hollow when done, filled teal with a halo while running,
 *  green for a pass, a red ring for a failure, a small square when stopped. */
export function RunRing({ mark }: { mark: RunMark }) {
    return (
        <i
            data-run-mark={mark}
            aria-hidden
            className={cn(
                'inline-block flex-none box-border not-italic',
                mark === 'stopped'
                    ? 'h-1.5 w-1.5 rounded-[0.09375rem] bg-ink-3'
                    : 'h-[0.4375rem] w-[0.4375rem] rounded-full border-[1.5px] border-solid',
                mark === 'done' && 'border-ink-3',
                mark === 'pass' && 'border-green bg-green',
                mark === 'fail' && 'border-red',
                mark === 'running' && 'border-teal bg-teal shadow-[0_0_0_3px_var(--teal-soft)]',
            )}
        />
    );
}

/** A version or an edit: a small dot; the current version's is teal. */
export function Pip({ current = false }: { current?: boolean }) {
    return (
        <i
            aria-hidden
            data-pip={current ? 'current' : ''}
            className={cn('inline-block flex-none rounded-full not-italic', current ? 'h-1.5 w-1.5 bg-teal' : 'h-[0.3125rem] w-[0.3125rem] bg-ink-2')}
        />
    );
}

const MOVE_PATHS: Partial<Record<JournalKind, ReactNode>> = {
    duplicate: (
        <>
            <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
            <path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
        </>
    ),
    merge: <path d="M3.5 8h9M8 2v4M8 14v-4M6.5 4.5 8 6l1.5-1.5M6.5 11.5 8 10l1.5 1.5" />,
    split: <path d="M8 3.5v9M5.5 8h-3M10.5 8h3M4 6.5 2.5 8 4 9.5M12 6.5 13.5 8 12 9.5" />,
};

/** A move's glyph: the copy, the merge, the fan-out. */
export function MoveGlyph({ kind }: { kind: 'duplicate' | 'merge' | 'split' }) {
    return (
        <svg
            data-move-glyph={kind}
            viewBox="0 0 16 16"
            aria-hidden
            className="h-3 w-3 flex-none"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
            strokeLinecap="round"
            strokeLinejoin="round"
        >
            {MOVE_PATHS[kind]}
        </svg>
    );
}

/** A file's name with its extension quieted (the cards' file rows): its
 *  list's label for it (`fileLabels`, a folder suffix before a shared base
 *  name), else its base name. */
export function FileName({ path, label = baseName(path) }: { path: string; label?: string }) {
    const cut = label.lastIndexOf('/') + 1;
    const dot = label.lastIndexOf('.');
    const [stem, ext] = dot > cut ? [label.slice(0, dot), label.slice(dot)] : [label, ''];
    return (
        <>
            {stem}
            {ext && <span className="font-mono text-[0.6875rem] text-ink-3">{ext}</span>}
        </>
    );
}

/** A card's file rows: each named as `fileLabels` tells it from the card's
 *  other files (all of `among`, shown or not). */
export function FileRows({ paths, among = paths }: { paths: readonly string[]; among?: readonly string[] }) {
    const named = fileLabels(among);
    const labels = new Map(among.map((path, i) => [path, named[i]] as const));
    return (
        <>
            {paths.map(path => <li key={path} className="truncate"><FileName path={path} label={labels.get(path)} /></li>)}
        </>
    );
}
