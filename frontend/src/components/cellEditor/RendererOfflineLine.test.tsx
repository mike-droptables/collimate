import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

vi.mock('@/lib/api/catalog', () => ({ fetchRendererOffline: vi.fn(), freezeRendererOffline: vi.fn() }));
import { fetchRendererOffline, freezeRendererOffline } from '@/lib/api/catalog';
import { RendererOfflineLine } from './RendererOfflineLine';

const missing = { complete: false, roots: 2, modules: 3, truncated: false,
    missing: [{ url: 'https://esm.sh/acme-chart@1.2.3/es2022/lazy.mjs', reason: 'upstream answered 404' }] };

beforeEach(() => { vi.resetAllMocks(); });

describe('RendererOfflineLine', () => {
    it('says a renderer whose whole graph is held works offline, with no store button', async () => {
        vi.mocked(fetchRendererOffline).mockResolvedValue({ complete: true, roots: 2, modules: 4, missing: [], truncated: false });
        render(<RendererOfflineLine rendererId="acme-chart" />);
        expect(await screen.findByText(/Works offline: every module it imports is on this installation \(2 roots\)/)).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Store for offline use' })).toBeNull();
        expect(fetchRendererOffline).toHaveBeenCalledWith('acme-chart');
    });

    it('lists what is not stored and stores it on request', async () => {
        vi.mocked(fetchRendererOffline).mockResolvedValue(missing);
        vi.mocked(freezeRendererOffline).mockResolvedValue({ ...missing, complete: true, missing: [] });
        render(<RendererOfflineLine rendererId="acme-chart" />);
        expect(await screen.findByText(/1 module it imports is not stored on this installation yet/)).toBeTruthy();
        expect(screen.getByText('https://esm.sh/acme-chart@1.2.3/es2022/lazy.mjs')).toBeTruthy();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Store for offline use' })); });
        expect(freezeRendererOffline).toHaveBeenCalledWith('acme-chart');
        expect(screen.getByText(/Works offline/)).toBeTruthy();
    });
});
