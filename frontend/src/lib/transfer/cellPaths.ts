// The cell paths an import never writes: a run's `_runs/` band and the
// workspace staging band `.collrun` (api/reserved_paths.py). The import
// commit refuses either with a 400, so every client path leaves them out
// before anything is uploaded.

import { isRunPath, normalizeRunPath } from '@/lib/runningCell';

/** The staging band's root. Mirrors api/reserved_paths.py RESERVED_PREFIX. */
const STAGING_PREFIX = '.collrun';

/** Whether `path`, relative to a cell, lies in the `_runs/` or `.collrun`
 *  band (the band's own folder counts). */
export function isReservedCellPath(path: string): boolean {
    if (isRunPath(path)) return true;
    const normalized = normalizeRunPath(path);
    return normalized === STAGING_PREFIX || normalized.startsWith(`${STAGING_PREFIX}/`);
}
