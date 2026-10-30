// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useRef } from 'react';
import type { Channel } from '@/lib/types';
import type { RemovedCell } from '@/lib/api/cells';

const cells = vi.hoisted(() => ({ fetchChannelTags: vi.fn() }));
vi.mock('@/lib/api/cells', async (original) => ({ ...await original<typeof import('@/lib/api/cells')>(), ...cells }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { RemoveZonesEditor } from './RemoveZonesEditor';
import { REMOVE_ZONES_MAX, type RemoveZone } from './removeZones';

const s = () => useCollimateStore.getState();
const zones = () => s().overview.removeZones;
const channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [{ id: 's1', name: 's1', cells: [{ id: 'a', name: 'A', files: [], tags: ['ui'] }] }],
} as Channel;
const shelved = { cell_id: 'x', tags: ['archive'] } as RemovedCell;

const outerClick = vi.fn();
function Harness() {
    const anchor = useRef<HTMLButtonElement>(null);
    return (
        <div onClick={outerClick}>
            <button ref={anchor} type="button">zones</button>
            <RemoveZonesEditor anchor={anchor} open onClose={() => {}} />
        </div>
    );
}

const mount = async (stored: RemoveZone[] = []) => {
    act(() => { s().adoptRemoveZones(stored); });
    render(<Harness />);
    await waitFor(() => expect(cells.fetchChannelTags).toHaveBeenCalledWith('chan'));
    return screen.getByRole('dialog', { name: 'Remove zones' });
};
const rows = () => [...document.querySelectorAll('[data-zone-row]')];
const TWO: RemoveZone[] = [
    { id: 'z1', width: 0.5, action: { kind: 'add_tag', tag: 'done' } },
    { id: 'z2', width: 0.5, action: { kind: 'remove' } },
];

beforeEach(() => {
    vi.clearAllMocks();
    cells.fetchChannelTags.mockResolvedValue([{ tag: 'done', live: 1, removed: 0 }, { tag: 'wip', live: 0, removed: 2 }]);
    // The board's channel first: a switch drops the overview's data.
    useCollimateStore.setState({ activeChannel: channel, overview: defaultOverviewView() });
    useCollimateStore.setState(prev => ({ overviewData: { ...prev.overviewData, channel: 'chan', removed: [shelved] } }));
});

describe('RemoveZonesEditor (TG-06)', () => {
    it('with no zones shows the one full-width plain remove; it cannot be deleted and there is nothing to reset (TG-06)', async () => {
        const dialog = await mount();
        expect(rows()).toHaveLength(1);
        expect(within(dialog).getByRole('combobox', { name: 'Zone 1 action' })).toHaveValue('remove');
        expect(within(dialog).getByRole('button', { name: 'Delete zone 1' })).toBeDisabled();
        expect(within(dialog).getByRole('button', { name: 'Reset to one zone' })).toBeDisabled();
        expect([...dialog.querySelectorAll('[data-zone-preview-segment]')].map(el => el.textContent)).toEqual(['Standard']);
        // The plain remove's option reads Standard; the kind stays "remove".
        const options = [...within(dialog).getByRole('combobox', { name: 'Zone 1 action' }).querySelectorAll('option')];
        expect(options.map(o => [o.value, o.textContent])).toEqual([
            ['remove', 'Standard'], ['add_tag', 'Add tag'], ['remove_tag', 'Remove tag'],
        ]);
        expect(dialog).toHaveTextContent('Standard (plain remove)');
    });

    it('Add zone splits the width evenly; an action and its tag set a zone, the tag normalised when the field is left (TG-06)', async () => {
        const dialog = await mount();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Add zone' }));
        expect(zones().map(z => z.width)).toEqual([0.5, 0.5]);
        fireEvent.change(within(dialog).getByRole('combobox', { name: 'Zone 2 action' }), { target: { value: 'add_tag' } });
        expect(zones()[1].action).toEqual({ kind: 'add_tag', tag: '' });
        const tag = within(dialog).getByRole('combobox', { name: 'Zone 2 tag' });
        fireEvent.change(tag, { target: { value: '  needs   review ' } });
        expect(zones()[1].action).toEqual({ kind: 'add_tag', tag: '  needs   review ' });
        fireEvent.blur(tag);
        expect(zones()[1].action).toEqual({ kind: 'add_tag', tag: 'needs review' });
        expect([...dialog.querySelectorAll('[data-zone-preview-segment]')].map(el => el.textContent)).toEqual(['Standard', '+ needs review']);
        // Remove tag keeps the tag; Standard drops it.
        fireEvent.change(within(dialog).getByRole('combobox', { name: 'Zone 2 action' }), { target: { value: 'remove_tag' } });
        expect(zones()[1].action).toEqual({ kind: 'remove_tag', tag: 'needs review' });
        fireEvent.change(within(dialog).getByRole('combobox', { name: 'Zone 2 action' }), { target: { value: 'remove' } });
        expect(zones()[1].action).toEqual({ kind: 'remove' });
        expect(within(dialog).queryByRole('combobox', { name: 'Zone 2 tag' })).toBeNull();
    });

    it('suggests the channel\'s tags: the route\'s, the board\'s and the shelf\'s (TG-06)', async () => {
        const dialog = await mount(TWO);
        const input = within(dialog).getByRole('combobox', { name: 'Zone 1 tag' });
        const list = document.getElementById(input.getAttribute('list')!)!;
        await waitFor(() => expect([...list.querySelectorAll('option')].map(o => o.value)).toEqual(['archive', 'done', 'ui', 'wip']));
    });

    it('moves a zone left or right and deletes one, its width going to the others (TG-06)', async () => {
        const dialog = await mount(TWO);
        expect(within(dialog).getByRole('button', { name: 'Move zone 1 left' })).toBeDisabled();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Move zone 1 right' }));
        expect(zones().map(z => z.id)).toEqual(['z2', 'z1']);
        expect(within(dialog).getByRole('button', { name: 'Move zone 2 right' })).toBeDisabled();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete zone 1' }));
        expect(zones()).toEqual([{ ...TWO[0], width: 1 }]);
    });

    it('a divider drags or steps with the arrows to resize, kept 8% from its neighbours (TG-06)', async () => {
        const dialog = await mount(TWO);
        const preview = dialog.querySelector<HTMLElement>('[data-zone-preview]')!;
        preview.getBoundingClientRect = () => ({ left: 100, right: 500, width: 400, top: 0, bottom: 32, height: 32, x: 100, y: 0, toJSON: () => ({}) }) as DOMRect;
        const divider = within(dialog).getByRole('separator', { name: 'Between zone 1 and zone 2' });
        expect(divider).toHaveAttribute('aria-valuenow', '50');
        fireEvent.keyDown(divider, { key: 'ArrowRight' });
        expect(zones().map(z => z.width)).toEqual([0.52, 0.48]);
        fireEvent.pointerDown(divider, { button: 0, clientX: 308, pointerId: 4 });
        fireEvent.pointerMove(divider, { clientX: 200, pointerId: 4 });
        expect(zones().map(z => z.width)).toEqual([0.25, 0.75]);
        fireEvent.pointerMove(divider, { clientX: 490, pointerId: 4 });
        expect(zones().map(z => z.width)).toEqual([0.92, 0.08]);
        fireEvent.pointerUp(divider, { clientX: 490, pointerId: 4 });
        fireEvent.pointerMove(divider, { clientX: 200, pointerId: 4 });
        expect(zones().map(z => z.width)).toEqual([0.92, 0.08]);
    });

    it('stops at six zones, and Reset goes back to one plain remove (TG-06)', async () => {
        const dialog = await mount(TWO);
        const add = within(dialog).getByRole('button', { name: 'Add zone' });
        for (let i = 2; i < REMOVE_ZONES_MAX; i++) fireEvent.click(add);
        expect(zones()).toHaveLength(REMOVE_ZONES_MAX);
        expect(add).toBeDisabled();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Reset to one zone' }));
        expect(zones()).toEqual([]);
        expect(rows()).toHaveLength(1);
    });

    it('a click inside does not reach the shelf behind it (TG-06)', async () => {
        const dialog = await mount(TWO);
        fireEvent.click(dialog.querySelector('[data-zone-preview]')!);
        fireEvent.click(within(dialog).getByRole('combobox', { name: 'Zone 1 tag' }));
        expect(outerClick).not.toHaveBeenCalled();
    });

    it('closing drops a tag zone left without a tag, as the server would, the others taking its width (TG-06)', async () => {
        const dialog = await mount(TWO);
        fireEvent.click(within(dialog).getByRole('button', { name: 'Add zone' }));
        fireEvent.change(within(dialog).getByRole('combobox', { name: 'Zone 3 action' }), { target: { value: 'remove_tag' } });
        expect(zones()[2].action).toEqual({ kind: 'remove_tag', tag: '' });
        fireEvent.keyDown(dialog, { key: 'Escape' });
        expect(zones().map(z => [z.id, z.action, z.width])).toEqual([
            ['z1', { kind: 'add_tag', tag: 'done' }, 0.5],
            ['z2', { kind: 'remove' }, 0.5],
        ]);
    });
});
