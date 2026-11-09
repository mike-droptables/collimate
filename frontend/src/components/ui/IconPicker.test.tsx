import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { IconPicker } from './IconPicker';
import { ICON_NAMES } from '@/lib/iconRegistry';
import { KEYBOARD_OWNER_ATTR, eventPathHasAttr } from '@/lib/keyboardOwner';
import { retiredOr } from '@/test/retiredTokens';

const openPicker = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole('button', { name: 'Pick an icon' }));
    return screen.getByPlaceholderText('Search icons…');
};

describe('IconPicker', () => {
    it('opens the popover on trigger click and focuses the search input', async () => {
        const user = userEvent.setup();
        render(<IconPicker onChange={() => {}} />);
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
        const search = await openPicker(user);
        // Every registry icon is shown unfiltered.
        expect(screen.getByRole('button', { name: 'zap' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'brain' })).toBeInTheDocument();
        // Focus lands via the post-open requestAnimationFrame.
        await waitFor(() => expect(search).toHaveFocus());
    });

    it('clicking the trigger again toggles the popover shut', async () => {
        const user = userEvent.setup();
        render(<IconPicker onChange={() => {}} title="Group icon" />);
        const trigger = screen.getByRole('button', { name: 'Group icon' });
        await user.click(trigger);
        expect(screen.getByPlaceholderText('Search icons…')).toBeInTheDocument();
        await user.click(trigger);
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
    });

    it('filters the grid by substring and shows the empty message on no match', async () => {
        const user = userEvent.setup();
        render(<IconPicker onChange={() => {}} />);
        const search = await openPicker(user);
        await user.type(search, 'brain');
        expect(screen.getByRole('button', { name: 'brain' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'zap' })).not.toBeInTheDocument();
        await user.clear(search);
        await user.type(search, 'no-such-icon-xyz');
        expect(screen.getByText('No icons match.')).toBeInTheDocument();
        expect(ICON_NAMES.some(n => n.includes('no-such-icon-xyz'))).toBe(false);
    });

    it('choosing an icon reports it, closes, and resets the query', async () => {
        const user = userEvent.setup();
        const onChange = vi.fn();
        render(<IconPicker onChange={onChange} />);
        const search = await openPicker(user);
        await user.type(search, 'zap');
        await user.click(screen.getByRole('button', { name: 'zap' }));
        expect(onChange).toHaveBeenCalledWith('zap');
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
        // Reopen: the previous query must not persist as a filter.
        await openPicker(user);
        expect(screen.getByRole('button', { name: 'brain' })).toBeInTheDocument();
    });

    it('shows the clear button only when a value is set, and clears to null', async () => {
        const user = userEvent.setup();
        const onChange = vi.fn();
        const { rerender } = render(<IconPicker value={null} onChange={onChange} />);
        await openPicker(user);
        expect(screen.queryByRole('button', { name: 'Clear icon' })).not.toBeInTheDocument();
        rerender(<IconPicker value="zap" onChange={onChange} />);
        await user.click(screen.getByRole('button', { name: 'Clear icon' }));
        expect(onChange).toHaveBeenCalledWith(null);
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
    });

    it('marks the current value as active in the grid, on brass (S04)', async () => {
        const user = userEvent.setup();
        render(<IconPicker value="brain" onChange={() => {}} />);
        await openPicker(user);
        const active = screen.getByRole('button', { name: 'brain' }).className;
        expect(active).toContain('ring-brass');
        expect(active).toContain('bg-brass-soft');
        expect(active).not.toMatch(retiredOr('white\\/'));
        expect(screen.getByRole('button', { name: 'zap' }).className).not.toContain('ring-1');
    });

    it('closes on Escape and on an outside mousedown, but not on an inside one', async () => {
        const user = userEvent.setup();
        render(<IconPicker onChange={() => {}} />);
        const search = await openPicker(user);
        // Inside interactions keep it open.
        fireEvent.mouseDown(search);
        expect(screen.getByPlaceholderText('Search icons…')).toBeInTheDocument();
        await user.keyboard('{Escape}');
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
        await openPicker(user);
        fireEvent.mouseDown(document.body);
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
    });

    it('owns Escape in the search box so the scene handler skips it', async () => {
        const user = userEvent.setup();
        render(<IconPicker onChange={() => {}} />);
        const search = await openPicker(user);
        await waitFor(() => expect(search).toHaveFocus());
        // The scene's window capture handler must see the popover own the key.
        const seen = vi.fn();
        const listener = (e: Event) => seen(eventPathHasAttr(e, KEYBOARD_OWNER_ATTR));
        window.addEventListener('keydown', listener, true);
        fireEvent.keyDown(search, { key: 'Escape' });
        window.removeEventListener('keydown', listener, true);
        expect(seen).toHaveBeenCalledWith(true);
        expect(screen.queryByPlaceholderText('Search icons…')).not.toBeInTheDocument();
    });
});
