import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DeleteChannelDialog } from './DeleteChannelDialog';
import { fetchChannelSize } from '@/lib/api/storage';

// The delete confirmation P8 opens from the tables (plan 1105 "The page"),
// against a mocked GET /api/channels/{id}/size.
vi.mock('@/lib/api/storage', () => ({ fetchChannelSize: vi.fn() }));

const CHANNEL = { id: 'c1', name: 'alpha', path: '/home/me/Collimate/Channels/c1' };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('DeleteChannelDialog (plan 1105 P7, for P8)', () => {
    it('states what it frees, from the size route, and arms Delete only once the name is typed', async () => {
        vi.mocked(fetchChannelSize).mockResolvedValue({ bytes: 3 * 1024 * 1024, files: 12, computed_at: '2026-11-05T10:00:00Z' });
        const onConfirm = vi.fn(async () => {});
        render(<DeleteChannelDialog channel={CHANNEL} onCancel={vi.fn()} onConfirm={onConfirm} />);
        expect(fetchChannelSize).toHaveBeenCalledWith('c1');
        await waitFor(() => expect(document.querySelector('[data-delete-frees]'))
            .toHaveTextContent('Deletes the channel and frees 3.0 MB (its folder /home/me/Collimate/Channels/c1) immediately.'));
        const del = screen.getByRole('button', { name: 'Delete' });
        expect(del).toBeDisabled();
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'alp' } });
        fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
        expect(onConfirm).not.toHaveBeenCalled();
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'alpha' } });
        fireEvent.click(del);
        await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    });

    it('names the folder alone when the size cannot be read, and keeps a refusal on screen', async () => {
        vi.mocked(fetchChannelSize).mockRejectedValue(new Error('Request failed (404)'));
        const onConfirm = vi.fn(async () => { throw new Error('A run is live in this channel'); });
        render(<DeleteChannelDialog channel={CHANNEL} onCancel={vi.fn()} onConfirm={onConfirm} />);
        await waitFor(() => expect(document.querySelector('[data-delete-frees]'))
            .toHaveTextContent('Deletes the channel and its folder /home/me/Collimate/Channels/c1 immediately.'));
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'alpha' } });
        fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('A run is live in this channel');
        expect(screen.getByRole('dialog', { name: 'Delete alpha' })).toBeInTheDocument();
    });

    it('Esc and Cancel cancel', () => {
        vi.mocked(fetchChannelSize).mockReturnValue(new Promise(() => {}));
        const onCancel = vi.fn();
        render(<DeleteChannelDialog channel={CHANNEL} onCancel={onCancel} onConfirm={vi.fn()} />);
        expect(document.querySelector('[data-delete-frees]')).toHaveTextContent('(reading its size…)');
        fireEvent.keyDown(document, { key: 'Escape' });
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onCancel).toHaveBeenCalledTimes(2);
    });
});
