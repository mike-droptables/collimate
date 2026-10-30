// The shutdown overlay sits on the canvas with secondary ink, so it reads on
// every skin, paper included.

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: (sel: (s: { setShuttingDown: () => void; fetchState: () => Promise<void> }) => unknown) =>
        sel({ setShuttingDown: () => {}, fetchState: async () => {} }),
}));

import { ShutdownOverlay } from './ShutdownOverlay';
import { retiredOr } from '@/test/retiredTokens';

describe('ShutdownOverlay', () => {
    it('paints the canvas and ink tiers, never an old token (S04)', () => {
        const { container } = render(<ShutdownOverlay />);
        expect(container.firstElementChild).toHaveClass('bg-canvas', 'text-ink');
        expect(screen.getByText('Shutting down…')).toHaveClass('text-ink-2');
        const painted = [...container.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});
