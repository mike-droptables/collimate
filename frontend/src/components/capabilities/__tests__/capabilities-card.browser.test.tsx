import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { CapabilitiesManageCard } from '../CapabilitiesManageCard';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(async () => Array.from({ length: 8 }, (_, at) => generator({ name: `synth-gen-${at}`, display_name: `Synth generator ${at}` }))),
    fetchRenderersWithCell: vi.fn(async () => [renderer(), builtinRenderer()]),
    fetchRendererPreferences: vi.fn(async () => ({})),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(async () => [runnerGenerator()]) }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn(async () => [handler(), builtinHandler()]) }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(async () => []) }));
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: [runner()], busy: false, error: null, refresh: async () => {} }) }));

beforeEach(() => {
    seedChannel();
});
afterEach(() => { document.body.innerHTML = ''; });

// Real Chromium: a vertical wheel over the manage list scrolls the list
// natively and goes no further: no ancestor (the stack's paging, the scene)
// sees it, so the stack's front slot never moves.
it('a wheel inside the list scrolls the list and does not page the stack', async () => {
    const paged = vi.fn();
    const onSelect = vi.fn();
    const view = await render(<div onWheel={paged} style={{ width: 420, height: 320, position: 'relative' }}>
        <CapabilitiesManageCard stackId="caps" width={420} height={320} isSelected isFront onSelect={onSelect} onShowCell={() => {}} onHide={() => {}} />
    </div>);
    try {
        await expect.element(page.getByText('Synth generator 7')).toBeInTheDocument();
        const list = page.getByText('What this channel can make, show and check.', { exact: false });
        const viewport = document.querySelector('[data-capabilities-list]')!.parentElement!.parentElement as HTMLElement;
        expect(viewport.hasAttribute('data-radix-scroll-area-viewport')).toBe(true);
        expect(viewport.scrollHeight).toBeGreaterThan(viewport.clientHeight);
        await list.wheel({ delta: { y: 120 } });
        await expect.poll(() => viewport.scrollTop).toBeGreaterThan(0);
        expect(paged).not.toHaveBeenCalled();
        expect(onSelect).not.toHaveBeenCalled();
    } finally {
        await view.unmount();
    }
});

it('a wheel over an unselected card selects the stack and still scrolls the list', async () => {
    const onSelect = vi.fn();
    const view = await render(<div style={{ width: 420, height: 320, position: 'relative' }}>
        <CapabilitiesManageCard stackId="caps" width={420} height={320} isSelected={false} isFront onSelect={onSelect} onShowCell={() => {}} onHide={() => {}} />
    </div>);
    try {
        await expect.element(page.getByText('Synth generator 7')).toBeInTheDocument();
        const viewport = document.querySelector('[data-capabilities-list]')!.parentElement!.parentElement as HTMLElement;
        await page.getByText('What this channel can make, show and check.', { exact: false }).wheel({ delta: { y: 120 } });
        await expect.poll(() => onSelect.mock.calls.length).toBe(1);
        await expect.poll(() => viewport.scrollTop).toBeGreaterThan(0);
    } finally {
        await view.unmount();
    }
});
