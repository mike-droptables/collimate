import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { btn } from '@/components/ui/formStyles';
import { EmptyTab } from './EmptyTab';

const button = () => screen.getByRole('button', { name: 'Open existing or create a new file' });

describe('EmptyTab', () => {
    beforeEach(() => {
        // No active channel: the opened session is never written through.
        useCollimateStore.setState({ activeChannel: null, pickSessions: {}, cellDiffs: {}, cellHistory: {} });
    });

    it('its one control, the shared quiet button, opens the cell\'s Files panel', () => {
        render(<EmptyTab cellId="cell-1" readOnly={false} />);
        expect(screen.getAllByRole('button')).toEqual([button()]);
        expect(button().className).toBe(btn);
        fireEvent.click(button());
        expect(useCollimateStore.getState().pickSessions['cell-1']).toMatchObject({ kind: 'files', query: '' });
    });

    it('keeps the drag hint', () => {
        render(<EmptyTab cellId="cell-1" readOnly={false} />);
        expect(screen.getByText('…or drag a tab here')).toBeTruthy();
    });

    it('a read-only cell takes no panel: the button is disabled and says why', () => {
        render(<EmptyTab cellId="cell-1" readOnly />);
        expect(button()).toBeDisabled();
        expect(button()).toHaveAttribute('title', 'This cell is read-only');
        fireEvent.click(button());
        expect(useCollimateStore.getState().pickSessions['cell-1']).toBeUndefined();
    });
});
