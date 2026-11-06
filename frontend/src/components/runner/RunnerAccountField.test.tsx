// @vitest-environment jsdom
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Account, AccountRequirement } from '@/lib/accountsApi';
import { RunnerAccountField } from './RunnerAccountField';

const structure = { url: 'public', auth: { user: 'public', token: 'secret' } } as const;
const role: AccountRequirement = { label: 'Issue tracker', kind: 'custom', structure };
const custom = (id: string, shape: unknown, extra: Partial<Account> = {}): Account => ({
    id, name: `Entry ${id}`, kind: 'custom', service: 'custom', protocol: 'custom', endpoint: '', model: '', capabilities: [],
    auth_type: 'none', metadata: { structure: shape, values: {} }, configured: true, ...extra,
});
const field = (accounts: Account[], onOpenKeys = vi.fn(), value = '') => render(<RunnerAccountField role="tracker" requirement={role}
    accounts={accounts} value={value} disabled={false} onChange={vi.fn()} onOpenKeys={onOpenKeys} />);

describe('a custom role in the runner picker', () => {
    it('lists exactly the entries with the structure it expects', () => {
        field([
            custom('same', { auth: { token: 'secret', user: 'public' }, url: 'public' }),
            custom('flag', { auth: { token: 'public', user: 'public' }, url: 'public' }),
            custom('flat', { token: 'secret', user: 'public', url: 'public' }),
            custom('extra', { ...structure, region: 'public' }),
        ]);
        const options = within(screen.getByRole('combobox', { name: 'Issue tracker' })).getAllByRole('option').map(option => option.textContent);
        expect(options).toEqual(['Choose an account…', 'Entry same']);
    });

    it('with no match shows the expected structure, keys and secret flags only, and offers it pre-shaped', () => {
        const onOpenKeys = vi.fn();
        field([custom('flat', { token: 'secret', user: 'public', url: 'public' })], onOpenKeys);
        expect(screen.queryByRole('combobox')).toBeNull();
        expect(screen.getByText('No Key manager entry has the structure this generator expects')).toBeInTheDocument();
        const tree = screen.getByRole('tree', { name: 'Structure Issue tracker expects' });
        const auth = within(tree).getByRole('treeitem', { name: 'auth, group' });
        expect(auth).toContainElement(within(tree).getByRole('treeitem', { name: 'token, secret' }));
        expect(auth).toContainElement(within(tree).getByRole('treeitem', { name: 'user, public' }));
        expect(within(tree).getByRole('treeitem', { name: 'url, public' })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Create this structure in the Key manager' }));
        expect(onOpenKeys).toHaveBeenLastCalledWith({ name: 'Issue tracker', structure });
        fireEvent.click(screen.getByRole('button', { name: 'Open Key manager' }));
        expect(onOpenKeys).toHaveBeenLastCalledWith();
    });

    it('never offers an incomplete entry, and marks a saved choice that no longer matches', () => {
        field([custom('same', structure, { configured: false })], vi.fn(), 'same');
        expect(screen.getByRole('option', { name: 'Unavailable account (same)' })).toBeInTheDocument();
        expect(screen.getByText(/no longer compatible/)).toBeInTheDocument();
    });
});
