/**
 * The interface scale (RS-05): this device's page zoom through the desktop
 * bridge, one of the offered values; nothing to set in a plain browser.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
    DEFAULT_INTERFACE_SCALE, INTERFACE_SCALES, canSetInterfaceScale, getInterfaceScale, interfaceScaleLabel,
    isInterfaceScale, setInterfaceScale,
} from '@/lib/interfaceScale';
import type { CollimateDesktop } from '@/lib/desktop';

const install = (members: Partial<CollimateDesktop>) => {
    (globalThis as { window?: unknown }).window = { collimateDesktop: members };
};

afterEach(() => {
    delete (globalThis as { window?: unknown }).window;
});

describe('interface scale (RS-05)', () => {
    it('offers sizes around the 100% default (RS-05)', () => {
        expect(INTERFACE_SCALES).toEqual([0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5]);
        expect(DEFAULT_INTERFACE_SCALE).toBe(1);
        expect(INTERFACE_SCALES.map(interfaceScaleLabel)).toEqual(['70%', '80%', '90%', '100%', '110%', '125%', '150%']);
        expect(isInterfaceScale(1.1)).toBe(true);
        for (const junk of [1.2, 0, '1', null]) expect(isInterfaceScale(junk)).toBe(false);
    });

    it('in a browser there is no scale to read or set (RS-05)', () => {
        expect(canSetInterfaceScale()).toBe(false);
        expect(getInterfaceScale()).toBeNull();
        expect(setInterfaceScale(1.1)).toBe(false);
        // A shell without the members (or with one) reads the same.
        install({ getInterfaceScale: () => 1.1 });
        expect(canSetInterfaceScale()).toBe(false);
        expect(getInterfaceScale()).toBeNull();
    });

    it('reads and sets through the desktop bridge, on-list only (RS-05)', () => {
        let current = 1.25;
        const set = vi.fn((f: number) => { current = f; });
        install({ getInterfaceScale: () => current, setInterfaceScale: set });
        expect(canSetInterfaceScale()).toBe(true);
        expect(getInterfaceScale()).toBe(1.25);
        expect(setInterfaceScale(0.9)).toBe(true);
        expect(set).toHaveBeenCalledExactlyOnceWith(0.9);
        expect(getInterfaceScale()).toBe(0.9);
        expect(setInterfaceScale(1.75)).toBe(false);
        expect(set).toHaveBeenCalledOnce();
        // A shell reporting an off-list value reads as the default.
        current = 3;
        expect(getInterfaceScale()).toBe(1);
    });
});
