// The interface scale: how large the whole app draws on this device, as the
// desktop shell's page zoom. The displayed 100% is Electron zoom 1.
// It is per device, not per channel: the shell keeps it in its own
// settings.json and re-applies it on every load. Its View menu's zoom items
// step the same list. A plain browser has no such control here (its own zoom
// does the job), so the Themes tab shows the row only when the shell exposes it.

import { desktopBridge } from '@/lib/desktop';

/** The scales on offer, smallest first (desktop/src/shared/bridge.ts
 *  INTERFACE_SCALES; the shell drops any other factor). */
export const INTERFACE_SCALES = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5] as const;
export type InterfaceScale = (typeof INTERFACE_SCALES)[number];
export const DEFAULT_INTERFACE_SCALE: InterfaceScale = 1;

export function isInterfaceScale(value: unknown): value is InterfaceScale {
    return typeof value === 'number' && (INTERFACE_SCALES as readonly number[]).includes(value);
}

/** Whether this window can set an interface scale (the desktop shell). */
export function canSetInterfaceScale(): boolean {
    const bridge = desktopBridge();
    return typeof bridge?.getInterfaceScale === 'function' && typeof bridge.setInterfaceScale === 'function';
}

/** The device's scale, or null where there is none to set (a browser). */
export function getInterfaceScale(): InterfaceScale | null {
    if (!canSetInterfaceScale()) return null;
    const scale = desktopBridge()!.getInterfaceScale!();
    return isInterfaceScale(scale) ? scale : DEFAULT_INTERFACE_SCALE;
}

/** Ask the shell to apply and persist `scale`. False (and nothing sent)
 *  off the list or outside the shell. */
export function setInterfaceScale(scale: number): boolean {
    if (!isInterfaceScale(scale) || !canSetInterfaceScale()) return false;
    desktopBridge()!.setInterfaceScale!(scale);
    return true;
}

/** The row's label for a scale: 1.1 → "110%". */
export function interfaceScaleLabel(scale: InterfaceScale): string {
    return `${Math.round(scale * 100)}%`;
}
