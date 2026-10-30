// The server-down screen polls for the backend coming back. The next poll
// waits for the previous one to settle, so a server that accepts
// connections but hangs doesn't collect a pile of pending requests.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';

const { fetchState } = vi.hoisted(() => ({ fetchState: vi.fn<() => Promise<void>>() }));

vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: (sel: (s: { fetchState: () => Promise<void> }) => unknown) => sel({ fetchState }),
}));

import { ServerDownScreen } from './ServerDownScreen';
import { retiredOr } from '@/test/retiredTokens';

const POLL_MS = 1500;

describe('ServerDownScreen polling', () => {
    beforeEach(() => { vi.useFakeTimers(); fetchState.mockReset(); });
    afterEach(() => { vi.useRealTimers(); });

    it('does not start a new poll while the previous one is pending', async () => {
        fetchState.mockImplementation(() => new Promise<void>(() => {}));
        render(<ServerDownScreen onOpenSettings={() => {}} />);
        await vi.advanceTimersByTimeAsync(3 * POLL_MS);
        expect(fetchState).toHaveBeenCalledTimes(1);
    });

    it('polls once per interval when each poll settles', async () => {
        fetchState.mockResolvedValue(undefined);
        const { unmount } = render(<ServerDownScreen onOpenSettings={() => {}} />);
        await vi.advanceTimersByTimeAsync(POLL_MS);
        await vi.advanceTimersByTimeAsync(POLL_MS);
        await vi.advanceTimersByTimeAsync(POLL_MS);
        expect(fetchState).toHaveBeenCalledTimes(3);
        unmount();
        await vi.advanceTimersByTimeAsync(3 * POLL_MS);
        expect(fetchState).toHaveBeenCalledTimes(3);
    });

    it('paints the offline screen from the skin tokens (S04)', () => {
        fetchState.mockResolvedValue(undefined);
        const { container, getByText, getByRole } = render(<ServerDownScreen onOpenSettings={() => {}} />);
        expect(container.firstElementChild).toHaveClass('bg-canvas', 'text-ink');
        expect(getByText('uvx collimate').parentElement).toHaveClass('bg-code-bg', 'border-line');
        expect(getByText('Watching for the server…').previousElementSibling).toHaveClass('bg-teal');
        expect(getByRole('button', { name: 'API settings' })).toHaveClass('border-line-2', 'text-ink-2');
        const painted = [...container.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});
