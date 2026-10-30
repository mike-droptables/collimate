import { render } from 'vitest-browser-react';
import { expect, it } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import '@/app/globals.css';
import type { RemovedCell } from '@/lib/api/cells';
import type { RemoveZone } from '@/lib/api/settings';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { OVERVIEW_CARD_HEIGHT, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { RemovedBar } from './RemovedBar';
import { RemovedShelf } from './RemovedShelf';

const removed = (id: string): RemovedCell => ({
    cell_id: id, tip_version_id: `${id}-v`, name: `Cell ${id}`, icon: null, stack_name: null, stack_cell_count: 1,
    reason: 'removed', created_at: '2026-10-01T10:00:00+00:00', edited_at: '2026-10-02T10:00:00+00:00',
    removed_at: '2026-10-03T10:00:00+00:00', made_by: { kind: 'user', runner_id: null, runner_name: null },
    files: ['SeedIdea.md'], file_count: 1, preview: { path: 'SeedIdea.md', text: '# A seed' }, tags: [],
});

const rect = (el: Element) => el.getBoundingClientRect();
/** A shelf card's height: the shelf less the control row and vertical padding. */
const fit = (shelf: number) => Math.min(OVERVIEW_CARD_HEIGHT,
    Math.max(130, shelf - 4 * Number.parseFloat(getComputedStyle(document.documentElement).fontSize)));

it('a real drag on the bar moves it, resizes the shelf and refits the cards, a short one toggles, and below 110px it shuts (O05)', async () => {
    await page.viewport(1000, 900);
    const prior = useCollimateStore.getState();
    useCollimateStore.setState({
        overview: { ...defaultOverviewView(), shelfOpen: true, shelfHeight: 300 },
        overviewData: { ...prior.overviewData, channel: 'chan', removed: ['x', 'y', 'z'].map(removed), removedMatches: null, lineage: null },
    });
    const view = await render(
        <div data-overview-root data-testid="overview-root" style={{ position: 'relative', width: 900, height: 800, overflow: 'hidden' }}>
            <RemovedShelf />
            <RemovedBar />
        </div>,
    );
    try {
        const root = document.querySelector<HTMLElement>('[data-overview-root]')!;
        const shelf = document.querySelector<HTMLElement>('[data-rm-shelf]')!;
        const bar = document.querySelector<HTMLElement>('[data-rm-bar]')!;
        const card = () => document.querySelector<HTMLElement>('[data-rm-card="x"]')!;
        /** A press on the bar's line, `dy` px down (real mouse, pointer capture and the click after it). */
        const dragBar = async (dy: number) => {
            const track = bar.querySelector<HTMLElement>('[data-rm-track]')!;
            const y = rect(track).top - rect(root).top + 9;
            await userEvent.dragAndDrop(page.getByRole('button', { name: /Removed cells/ }), page.getByTestId('overview-root'), {
                sourcePosition: { x: 200, y: 9 },
                targetPosition: { x: 218, y: y + dy },
                steps: 6,
            });
        };

        await expect.poll(() => rect(shelf).height).toBe(300);
        const top = rect(root).top;
        expect(rect(bar).top - top).toBe(300);
        expect(Math.abs(rect(card()).height - fit(300))).toBeLessThanOrEqual(1 / 64);
        // The preview file's extension stands apart from its stem by the spacing token.
        const ext = document.querySelector<HTMLElement>('[data-rm-card="x"] [data-rm-preview-name] span > span')!;
        expect(ext.textContent).toBe('.md');
        expect(Number.parseFloat(getComputedStyle(ext).marginLeft)).toBeCloseTo(
            0.375 * Number.parseFloat(getComputedStyle(document.documentElement).fontSize), 1,
        );

        // Up by 80: the shelf, the bar and the cards follow; no toggle.
        await dragBar(-80);
        expect(rect(shelf).height).toBe(220);
        expect(rect(bar).top - top).toBe(220);
        expect(useCollimateStore.getState().overview).toMatchObject({ shelfOpen: true, shelfHeight: 220 });
        expect(Math.abs(rect(card()).height - fit(220))).toBeLessThanOrEqual(1 / 64);
        expect(root.style.getPropertyValue('--ov-shelf')).toBe('220px');

        // A press that moves 3px is a click: it shuts the shelf, the height is kept.
        await dragBar(3);
        expect(useCollimateStore.getState().overview).toMatchObject({ shelfOpen: false, shelfHeight: 220 });
        await expect.poll(() => rect(shelf).height).toBe(0);
        expect(rect(bar).top - top).toBe(0);

        // A drag opens it again from shut, to where it is let go.
        await dragBar(260);
        expect(useCollimateStore.getState().overview).toMatchObject({ shelfOpen: true, shelfHeight: 260 });
        expect(rect(shelf).height).toBe(260);
        expect(Math.abs(rect(card()).height - fit(260))).toBeLessThanOrEqual(1 / 64);

        // Let go below 110px: it shuts, and keeps the last kept height.
        await dragBar(-170);
        expect(useCollimateStore.getState().overview).toMatchObject({ shelfOpen: false, shelfHeight: 260 });
        await expect.poll(() => rect(shelf).height).toBe(0);
        await expect.poll(() => rect(bar).top - top).toBe(0);
    } finally {
        await view.unmount();
        useCollimateStore.setState({ overview: defaultOverviewView(), overviewData: prior.overviewData });
    }
}, 15000);

it('while a removable card is held the bar and the shelf split into the same zones, the one under the card lit (TG-06)', async () => {
    await page.viewport(1000, 900);
    const prior = useCollimateStore.getState();
    const priorSettings = useChannelSettingsStore.getState();
    const zones: RemoveZone[] = [
        { id: 'z1', width: 0.25, action: { kind: 'add_tag', tag: 'done' } },
        { id: 'z2', width: 0.5, action: { kind: 'remove' } },
        { id: 'z3', width: 0.25, action: { kind: 'remove_tag', tag: 'wip' } },
    ];
    // The channel's settings document brings the zones (the bar adopts them).
    useChannelSettingsStore.setState({
        loadedFor: 'chan',
        doc: {
            version: 1, kind: 'channel-settings', keyboard: { overrides: {}, commands: {} },
            theme: { activeThemeId: null, userThemes: [] }, editing: { spellcheck: true },
            layout: { defaultStackWidth: null, removedShelfHeight: 300 }, remove_zones: { zones },
        },
    });
    useCollimateStore.setState({
        overview: { ...defaultOverviewView(), shelfOpen: true, shelfHeight: 300 },
        overviewData: { ...prior.overviewData, channel: 'chan', removed: ['x'].map(removed), removedMatches: null, lineage: null },
    });
    const view = await render(
        <div data-overview-root style={{ position: 'relative', width: 900, height: 800, overflow: 'hidden' }}>
            <RemovedShelf />
            <RemovedBar />
        </div>,
    );
    try {
        const bar = document.querySelector<HTMLElement>('[data-rm-bar]')!;
        const barStrip = () => bar.querySelector<HTMLElement>('[data-rm-zone-strip]')!;
        // At rest: two thin dividers at a quarter and three quarters of the bar.
        const dividers = [...barStrip().querySelectorAll<HTMLElement>('[data-rm-zone-divider]')];
        expect(dividers.map(el => Math.round(rect(el).left - rect(barStrip()).left))).toEqual([225, 675]);

        useCollimateStore.getState().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 2, refusal: null });
        await expect.poll(() => document.querySelector('[data-rm-shelf] [data-rm-zone-strip]')).not.toBeNull();
        const shelfStrip = document.querySelector<HTMLElement>('[data-rm-shelf] [data-rm-zone-strip]')!;
        // One extent for both, so the drop resolves against either.
        expect(rect(shelfStrip).left).toBe(rect(barStrip()).left);
        expect(rect(shelfStrip).right).toBe(rect(barStrip()).right);
        const widths = (strip: HTMLElement) => [...strip.querySelectorAll<HTMLElement>('[data-rm-zone]')].map(el => Math.round(rect(el).width));
        expect(widths(barStrip())).toEqual([225, 450, 225]);
        expect(widths(shelfStrip)).toEqual([225, 450, 225]);
        expect([...barStrip().querySelectorAll('[data-rm-zone]')].map(el => el.textContent)).toEqual(['+ done', 'Standard', '\u2212 wip']);
        expect(shelfStrip.querySelector('[data-lit]')).toHaveAttribute('data-rm-zone', '2');
        expect(barStrip().querySelector('[data-lit]')).toBeNull();
        // The labelled bar lies over the bar's own label while the card is held.
        expect(getComputedStyle(barStrip()).zIndex).toBe('1');
        expect(getComputedStyle(barStrip().querySelector<HTMLElement>('[data-rm-zone="0"]')!).backgroundColor)
            .toBe(getComputedStyle(bar).backgroundColor);

        useCollimateStore.getState().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'bar', zone: 1, refusal: null });
        await expect.poll(() => barStrip().querySelector('[data-lit]')?.getAttribute('data-rm-zone')).toBe('1');
        useCollimateStore.getState().setOverviewDrop(null);
        await expect.poll(() => barStrip().querySelectorAll('[data-rm-zone]').length).toBe(0);
        expect(getComputedStyle(barStrip()).zIndex).toBe('auto');
    } finally {
        await view.unmount();
        useChannelSettingsStore.setState({ loadedFor: priorSettings.loadedFor, doc: priorSettings.doc });
        useCollimateStore.setState({ overview: defaultOverviewView(), overviewData: prior.overviewData });
    }
}, 15000);
