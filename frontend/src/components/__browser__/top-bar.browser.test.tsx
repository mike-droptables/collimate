import { render } from 'vitest-browser-react';
import { expect, it, vi } from 'vitest';
import { px } from '@/test/rootFontSize';
import { page, userEvent } from 'vitest/browser';
import { useState } from 'react';
import '@/app/globals.css';
import { AppTopBar } from '../AppTopBar';
import type { Channel, Stack } from '@/lib/types';

const channel: Channel = { id: 'bar', name: 'offline-sync', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null };
const capabilities: Stack = { id: 'cap', name: 'Capabilities', stack_type: 'capabilities', cells: [] };
const columns: Stack[] = Array.from({ length: 60 }, (_, i) => ({ id: `s${i}`, name: `Stack ${i}`, cells: [{ id: `c${i}`, name: `c${i}`, files: [] }] }));

function Harness({ onSelect, channels = [channel] }: { onSelect: (id: string) => void; channels?: Channel[] }) {
    const [selected, setSelected] = useState('s0');
    const noop = () => {};
    return (
        <div style={{ width: 1200 }}>
            <AppTopBar
                activeChannel={channel} channels={channels}
                columns={columns} selectedColumnId={selected} visibleTopIndices={{}} fanName={null}
                onSelectColumn={(id) => { onSelect(id); setSelected(id); }}
                isZoomedOut={false} toggleZoom={noop}
                capabilitiesStack={capabilities} capabilitiesShown onToggleCapabilities={noop}
                onNewStack={noop} commandsOpen={false} onCommands={noop}
                onSelectChannel={noop} onSelectServerChannel={noop} onRequestNewChannel={noop} onRenameChannel={noop} onDeleteChannel={noop} onOpenSettings={noop}
            />
        </div>
    );
}

it('is 2.25rem tall, keeps the scrubber centred and windowed between its sides, and the knob drags through the pips (C01) (C03)', async () => {
    await page.viewport(1300, 400);
    const onSelect = vi.fn();
    const view = await render(<Harness onSelect={onSelect} />);
    try {
        const bar = document.querySelector<HTMLElement>('[data-top-bar]')!;
        const [left, centre, right] = [...bar.children] as HTMLElement[];
        const box = bar.getBoundingClientRect();
        expect(box.height).toBeCloseTo(px(2.25), 1);
        expect(box.width).toBe(1200);

        // The right side holds one stack pill, Capabilities.
        const pills = [...right.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')].filter(b => b.textContent);
        expect(pills.map(b => b.textContent)).toEqual(['Capabilities']);
        expect(pills[0].title).toBe('Capabilities: open on the canvas. Click to hide.');

        // The pips window to the room between the sides: 60 stacks do not fit.
        const track = document.querySelector<HTMLElement>('[role="group"][aria-label="Stacks in this channel"]')!;
        const pipCount = () => track.querySelectorAll('[data-pip]').length;
        await expect.poll(pipCount).toBeLessThan(60);
        expect(pipCount()).toBeGreaterThan(5);
        const c = centre.getBoundingClientRect();
        expect(c.left).toBeGreaterThanOrEqual(left.getBoundingClientRect().right);
        expect(c.right).toBeLessThanOrEqual(right.getBoundingClientRect().left);
        expect(Math.abs((c.left + c.right) / 2 - (box.left + box.right) / 2)).toBeLessThan(1);

        // Pips sit on adjoining slots of one width; over the current one, the 0.5625rem brass knob.
        const pips = [...track.querySelectorAll<HTMLElement>('[data-pip]')];
        expect(pips[1].getBoundingClientRect().left - pips[0].getBoundingClientRect().left).toBe(pips[0].getBoundingClientRect().width);
        const knob = track.querySelector<HTMLElement>('[data-pip-knob]')!;
        const knobDot = knob.firstElementChild as HTMLElement;
        expect(knobDot.getBoundingClientRect().width).toBeCloseTo(px(0.5625), 1);
        const mid = (el: Element) => { const r = el.getBoundingClientRect(); return r.left + r.width / 2; };
        expect(mid(knobDot)).toBeCloseTo(mid(pips[0]), 0);
        expect(getComputedStyle(knobDot).backgroundColor).not.toBe('rgba(0, 0, 0, 0)');
        // The knob slides to a new pip (C03, polish).
        expect(getComputedStyle(knob).transitionProperty).toContain('transform');

        // Drag the knob three slots right: it selects each pip it passes.
        const t = track.getBoundingClientRect();
        const at = (k: number) => t.left + 15 * k + 7;
        const fire = (el: Element, type: string, x: number) => el.dispatchEvent(new PointerEvent(type, {
            bubbles: true, cancelable: true, button: 0, buttons: type === 'pointerup' ? 0 : 1, pointerId: 3, clientX: x, clientY: t.top + 18,
        }));
        fire(pips[0], 'pointerdown', at(0));
        for (const k of [1, 2, 3]) fire(track, 'pointermove', at(k));
        fire(track, 'pointerup', at(3));
        await expect.poll(() => onSelect.mock.calls.map(([id]) => id)).toEqual(['s1', 's2', 's3']);
        await expect.poll(() => track.querySelector('[aria-current="true"]')?.getAttribute('aria-label')).toBe('Stack 3');
        // Once its slide settles the knob stands on the new pip.
        await expect.poll(() => Math.round(mid(knobDot) - mid(pips[3]))).toBe(0);
    } finally {
        view.unmount();
    }
});

it('a 40-channel menu stays inside the window and scrolls; its last row, Settings…, is still reachable by keyboard (C02, polish)', async () => {
    await page.viewport(1300, 400);
    const channels: Channel[] = [channel, ...Array.from({ length: 39 }, (_, i) => ({ ...channel, id: `ch${i}`, name: `channel-${i}` }))];
    const view = await render(<Harness onSelect={() => {}} channels={channels} />);
    try {
        const button = document.querySelector<HTMLElement>('button[title="Channels"]')!;
        button.click();
        const findMenu = () => document.querySelector<HTMLElement>('[role="menu"][aria-label="Channels"]');
        await expect.poll(findMenu).toBeTruthy();
        const menu = findMenu()!;
        // Parked off-screen until measured.
        await expect.poll(() => menu.getBoundingClientRect().top).toBeGreaterThan(0);
        // Measured once the entry zoom has finished: mid-animation the rect
        // is scaled down.
        await expect.poll(() => menu.getAnimations().length).toBe(0);
        const box = menu.getBoundingClientRect();
        // Capped to the room under its button: it neither runs past the
        // window nor flips up over the bar.
        expect(box.top).toBeGreaterThanOrEqual(button.getBoundingClientRect().bottom);
        expect(box.bottom).toBeLessThanOrEqual(window.innerHeight);
        expect(menu.scrollHeight).toBeGreaterThan(menu.clientHeight);

        await userEvent.keyboard('{End}');
        const settings = menu.querySelector<HTMLElement>('[data-menu-item="settings"]')!;
        expect(document.activeElement).toBe(settings);
        const r = settings.getBoundingClientRect();
        expect(r.top).toBeGreaterThanOrEqual(box.top);
        expect(r.bottom).toBeLessThanOrEqual(box.bottom);
    } finally {
        view.unmount();
    }
});
