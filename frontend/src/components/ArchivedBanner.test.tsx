import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ArchivedBanner } from './ArchivedBanner';

afterEach(cleanup);

describe('ArchivedBanner (plan 1105 P7, D11)', () => {
    it('says the channel is archived and read-only, with Unarchive acting at once and Manage channels', async () => {
        const onUnarchive = vi.fn(async () => {});
        const onManage = vi.fn();
        render(<ArchivedBanner onUnarchive={onUnarchive} onManage={onManage} />);
        const banner = screen.getByRole('region', { name: 'Archived channel' });
        expect(banner).toHaveTextContent('Archived · read-only.');
        expect(banner).toHaveTextContent('Unsaved drafts stay, and save once it is unarchived.');
        fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }));
        await waitFor(() => expect(onUnarchive).toHaveBeenCalledOnce());
        fireEvent.click(screen.getByRole('button', { name: 'Manage channels' }));
        expect(onManage).toHaveBeenCalledOnce();
    });

    it('shows a refused Unarchive', async () => {
        render(<ArchivedBanner onUnarchive={vi.fn(async () => { throw new Error('The channel is moving'); })} onManage={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('The channel is moving');
        expect(screen.getByRole('button', { name: 'Unarchive' })).toBeEnabled();
    });
});
