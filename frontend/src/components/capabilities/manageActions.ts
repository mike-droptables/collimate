import type { KeyboardEvent } from 'react';
import { RENDERER_PREFS_CHANGED_EVENT, saveRendererPreferences } from '@/lib/api';

/** Esc goes back from a view that replaces the manage list, unless
 *  something inside took it (a menu, a modal). */
export function escapeGoesBack(e: KeyboardEvent, onBack: () => void): void {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if ((e.target as Element | null)?.closest?.('[aria-modal="true"]')) return;
    e.preventDefault();
    onBack();
}

/** Tell every listener (the manage view's catalogs, the renderer resolver)
 *  that renderer availability, preferences or display changed. */
export const announceRendererChange = () => window.dispatchEvent(new Event(RENDERER_PREFS_CHANGED_EVENT));

/** Make `rendererId` the channel's default for `ext`, over `prefs`. */
export async function makeDefaultRenderer(prefs: Record<string, string>, ext: string, rendererId: string): Promise<void> {
    try { await saveRendererPreferences({ ...prefs, [ext]: rendererId }); }
    finally { announceRendererChange(); }
}
