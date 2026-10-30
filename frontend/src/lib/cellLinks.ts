// A link in an annotation message's markdown names a cell file when it is a
// relative path (`notes/a.md`, `./b.md#x`, `/c.md`); the message view opens
// those in the cell (components/MarkdownView with its `cell` prop).

import { normalizeRunPath } from '@/lib/runningCell';

/** The cell-relative path an href names, or null for anything else: a URL
 *  with a scheme, a protocol-relative or fragment-only href, an empty one,
 *  or a path that climbs out of the cell. */
export function cellPathOfHref(href: string | undefined | null): string | null {
    const raw = href?.trim();
    if (!raw || raw.startsWith('#') || raw.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) return null;
    let path = raw.split('#')[0].split('?')[0];
    try {
        path = decodeURIComponent(path);
    } catch {
        // A malformed escape: the raw text is the path.
    }
    const normalized = normalizeRunPath(path.replace(/^\/+/, ''));
    if (!normalized || normalized === '..' || normalized.startsWith('../')) return null;
    return normalized;
}
