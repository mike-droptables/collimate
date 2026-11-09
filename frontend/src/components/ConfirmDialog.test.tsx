import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, act, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDialog } from './ConfirmDialog';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { retiredOr } from '@/test/retiredTokens';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));

describe('a cell\'s confirm', () => {
    afterEach(() => {
        cleanup();
        useCollimateStore.setState({ confirmDialog: null } as never);
    });

    it('shows in the cell\'s host as a modal of the cell, not in the app\'s instance, and resolves there', async () => {
        render(<ConfirmDialog />);
        const { container } = render(<div data-testid="pane-region"><ConfirmDialog cellId="cell-1" /></div>);
        let result: Promise<boolean> | undefined;
        act(() => { result = useCollimateStore.getState().openConfirm({ title: 'Delete file', body: 'Delete `notes.md`?', cellId: 'cell-1', destructive: true, confirmLabel: 'Delete' }); });
        const dialog = screen.getByRole('dialog');
        // In the cell's region, absolutely over it, a keyboard owner; never portalled to the body.
        expect(container.querySelector('[data-testid="pane-region"]')!.contains(dialog)).toBe(true);
        expect(dialog.parentElement!.getAttribute('data-confirm-host')).toBe('cell-1');
        expect(dialog.parentElement!.className).toMatch(/\babsolute\b/);
        expect(dialog.parentElement!.className).not.toMatch(/\bfixed\b/);
        expect(dialog.hasAttribute('data-owns-keyboard')).toBe(true);
        expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
        await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
        await expect(result).resolves.toBe(true);
    });

    it('another cell\'s host shows nothing; a dialog about no cell shows in the app\'s instance only', () => {
        render(<ConfirmDialog />);
        render(<ConfirmDialog cellId="cell-2" />);
        act(() => { void useCollimateStore.getState().openConfirm({ title: 'Delete file', body: 'Gone', cellId: 'cell-1' }); });
        // No host for cell-1 is mounted: the app's instance stands in, portalled to the body.
        expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
        expect(screen.getByRole('dialog').parentElement!.getAttribute('data-confirm-host')).toBe('app');
        expect(screen.getByRole('dialog').parentElement!.className).toMatch(/\bfixed\b/);
        act(() => { useCollimateStore.getState().resolveConfirm(false); });
        act(() => { void useCollimateStore.getState().openConfirm({ title: 'Reset', body: 'Everything' }); });
        expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
        expect(screen.getByRole('dialog').parentElement!.getAttribute('data-confirm-host')).toBe('app');
    });

    it('a host mounting after the question takes it over from the app\'s instance', async () => {
        const hosts = () => [...document.querySelectorAll('[data-confirm-host]')].map(node => node.getAttribute('data-confirm-host'));
        render(<ConfirmDialog />);
        act(() => { void useCollimateStore.getState().openConfirm({ title: 'Delete file', body: 'Gone', cellId: 'cell-1' }); });
        expect(hosts()).toEqual(['app']);
        const host = render(<ConfirmDialog cellId="cell-1" />);
        // The app's copy leaves through its exit motion; the cell's is the one that stays.
        await waitFor(() => expect(hosts()).toEqual(['cell-1']));
        host.unmount();
        await waitFor(() => expect(hosts()).toEqual(['app']));
    });
});

describe('ConfirmDialog Enter handling', () => {
    afterEach(() => {
        cleanup();
        useCollimateStore.setState({ confirmDialog: null } as never);
    });

    it('Enter on a focused Cancel button cancels, not confirms', async () => {
        const user = userEvent.setup();
        render(<ConfirmDialog />);
        let result!: Promise<boolean>;
        act(() => { result = useCollimateStore.getState().openConfirm({ title: 'Delete folder', body: 'Gone', destructive: true }); });
        screen.getByRole('button', { name: 'Cancel' }).focus();
        await user.keyboard('{Enter}');
        await expect(result).resolves.toBe(false);
    });

    it('Enter on the focused primary confirms', async () => {
        const user = userEvent.setup();
        render(<ConfirmDialog />);
        let result!: Promise<boolean>;
        act(() => { result = useCollimateStore.getState().openConfirm({ title: 'Delete folder', body: 'Gone', destructive: true }); });
        screen.getByRole('button', { name: 'Confirm' }).focus();
        await user.keyboard('{Enter}');
        await expect(result).resolves.toBe(true);
    });

    it('a destructive confirm paints red on the skin tokens and a plain one the primary (S04)', () => {
        render(<ConfirmDialog />);
        act(() => { void useCollimateStore.getState().openConfirm({ title: 'Delete folder', body: 'Gone', destructive: true }); });
        const dialog = screen.getByRole('dialog');
        expect(dialog).toHaveClass('bg-sheet', 'border-line-2', 'shadow-(--shadow)');
        expect(dialog.parentElement).toHaveClass('bg-(--lift)');
        expect(screen.getByRole('button', { name: 'Confirm' })).toHaveClass('text-red', 'hover:bg-red-soft');
        expect(screen.getByRole('button', { name: 'Cancel' })).toHaveClass('border-line-2', 'text-ink-2');
        expect(screen.getByText('Gone')).toHaveClass('text-ink-2');
        const painted = [...document.body.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\(', 'bg-destructive', 'bg-primary'));
        act(() => { useCollimateStore.getState().resolveConfirm(false); });
    });

    it("a plain confirm's primary uses the shared primary look (S04)", () => {
        render(<ConfirmDialog />);
        act(() => { void useCollimateStore.getState().openConfirm({ title: 'Name', body: '' }); });
        expect(screen.queryByRole('textbox')).toBeNull();
        expect(screen.getByRole('button', { name: 'Confirm' })).toHaveClass('bg-sheet', 'text-ink', '[&_svg]:text-brass');
        act(() => { useCollimateStore.getState().resolveConfirm(false); });
    });
});
