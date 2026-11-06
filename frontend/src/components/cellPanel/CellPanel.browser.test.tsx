import { expect, it } from 'vitest';
import { render } from 'vitest-browser-react';
import { Plus } from 'lucide-react';
import '@/app/globals.css';
import { CellPanel } from './CellPanel';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { PlayGlyph } from '@/components/ui/PlayGlyph';

/** A pane region of the given width: the positioned box the cell editor
 *  mounts a panel in (from under the cell head to the cell's bottom). */
const TWO_MODES = [{ id: 'run', label: 'Run' }, { id: 'edit', label: 'Edit' }];

async function mount(width: number, selected = false, modes = TWO_MODES) {
    const view = await render(
        <CellSelectionContext.Provider value={selected}>
            <div data-testid="region" style={{ position: 'relative', width, height: 520 }}>
                <CellPanel
                    cellId="cell-1" kind="runners" title="Runners"
                    note="Several runners in one cell can collide on the files they work on."
                    modes={modes} mode="run" onMode={() => {}}
                    query="" onQuery={() => {}} placeholder="Search runners"
                    searchActions={[{ id: 'runs', label: 'Runs', icon: Plus, onRun: () => {} }]}
                    rows={[{ id: 'a', name: 'seed-ideas', description: 'Writes one SeedIdea.md per option' }, { id: 'b', name: 'challenge' }]}
                    selectedId="a" onSelect={() => {}}
                    primary={{ label: 'Run ‘seed-ideas’', icon: PlayGlyph, intent: 'run', onRun: () => {} }}
                    secondary={{ label: 'in new cell', hint: '⇧↵', onRun: () => {} }}
                    onClose={() => {}}
                />
            </div>
        </CellSelectionContext.Provider>,
    );
    const region = document.querySelector<HTMLElement>('[data-testid="region"]')!;
    const panel = region.querySelector<HTMLElement>('[role="dialog"]')!;
    const q = <T extends Element = HTMLElement>(selector: string) => panel.querySelector<T>(selector)! as unknown as HTMLElement;
    const button = (name: string) => [...panel.querySelectorAll('button')].find(b => b.textContent === name)!;
    return { view, region, panel, q, button };
}

const box = (el: Element) => el.getBoundingClientRect();
const designPx = (value: number) => value / 16 * parseFloat(getComputedStyle(document.documentElement).fontSize);
const varBackground = (name: string) => {
    const probe = document.createElement('span');
    probe.style.backgroundColor = `var(${name})`;
    document.body.append(probe);
    const colour = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return colour;
};
const varColour = (name: string) => {
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
};

it('lays the panel out as the mock: 12px sides, 24px top and bottom over a z 4 scrim, a 36px head, 30px modes and search, a 40px foot (P01, RS-05)', async () => {
    const { view, region, panel, q } = await mount(420);
    const r = box(region), p = box(panel);
    for (const side of [p.left - r.left, r.right - p.right]) expect(side).toBeCloseTo(designPx(12), 1);
    expect(p.top - r.top).toBeCloseTo(designPx(24), 1);
    expect(r.bottom - p.bottom).toBeCloseTo(designPx(24), 1);
    const scrim = region.querySelector('[data-cell-panel-scrim]')!;
    expect(box(scrim)).toMatchObject({ width: r.width, height: r.height });
    expect(getComputedStyle(scrim).backgroundColor).toBe(varBackground('--lift'));
    expect(getComputedStyle(scrim).zIndex).toBe('4');
    expect(getComputedStyle(panel).zIndex).toBe('5');
    expect(getComputedStyle(panel).borderRadius).toBe('8px');
    const title = panel.firstElementChild!.firstElementChild!;
    expect(title.textContent).toBe('Runners');
    expect(getComputedStyle(title).textTransform).toBe('uppercase');
    expect(box(panel.firstElementChild!).height).toBeCloseTo(designPx(36), 1);
    expect(box(q('[role="group"]').parentElement!).height).toBeCloseTo(designPx(30), 1);
    expect(box(q('[role="combobox"]').closest('label')!).height).toBeCloseTo(designPx(30), 1);
    const primary = q('[data-testid="cell-panel-primary"]');
    expect(box(primary.parentElement!.parentElement!).height).toBeCloseTo(designPx(40), 1);
    expect(box(primary).height).toBeCloseTo(designPx(26), 1);
    await view.unmount();
});

it('caps the sheet at 640px, centred in a wide region, and keeps the 12px buffer as the region narrows (RS-05)', async () => {
    for (const width of [1200, 664, 652, 500]) {
        const { view, region, panel } = await mount(width);
        const r = box(region), p = box(panel);
        const expected = Math.min(designPx(640), width - 2 * designPx(12));
        expect(p.width, String(width)).toBeCloseTo(expected, 1);
        expect(p.left - r.left, String(width)).toBeCloseTo((width - expected) / 2, 1);
        expect(r.right - p.right, String(width)).toBeCloseTo((width - expected) / 2, 1);
        expect(getComputedStyle(panel).transform, String(width)).toBe('none');
        await view.unmount();
    }
});

it('rules the active mode brass in the selected cell and teal elsewhere (P01)', async () => {
    const rule = async (selected: boolean) => {
        const { view, button } = await mount(420, selected);
        const colour = getComputedStyle(button('Run'), '::after').backgroundColor;
        await view.unmount();
        return colour;
    };
    expect(await rule(true)).toBe(varColour('--brass'));
    expect(await rule(false)).toBe(varColour('--teal'));
    expect(varColour('--brass')).not.toBe(varColour('--teal'));
});

it('below 300px wide the action and secondary labels and the footer hints go, and the primary keeps the right edge (P01)', async () => {
    const wide = await mount(420);
    const label = (m: typeof wide) => m.q('button[aria-label="Runs"] span');
    expect(getComputedStyle(label(wide)).display).not.toBe('none');
    expect(getComputedStyle(wide.q('[data-cell-panel-hints]')).display).not.toBe('none');
    await wide.view.unmount();
    const narrow = await mount(280);
    expect(getComputedStyle(label(narrow)).display).toBe('none');
    expect(getComputedStyle(narrow.q('[data-cell-panel-hints]')).display).toBe('none');
    expect(getComputedStyle(narrow.q('[data-testid="cell-panel-secondary"] span')).display).toBe('none');
    const primary = box(narrow.q('[data-testid="cell-panel-primary"]'));
    expect(box(narrow.panel).right - primary.right).toBeLessThanOrEqual(10);
    await narrow.view.unmount();
});

it('sets the mode tabs at the panel\'s 13px and drops the "tab to toggle" hint whole when the tabs need its room (P01)', async () => {
    const roomy = await mount(420);
    const row = (m: typeof roomy) => m.q('[role="group"]').parentElement!;
    const hint = (m: typeof roomy) => m.q('[data-cell-panel-mode-hint]');
    expect(parseFloat(getComputedStyle(roomy.button('Run')).fontSize)).toBeCloseTo(designPx(13), 3);
    expect(box(hint(roomy)).bottom).toBeLessThanOrEqual(box(row(roomy)).bottom);
    expect(box(hint(roomy)).width).toBeGreaterThan(60);
    await roomy.view.unmount();
    // Six modes in a region just wide enough for them need the hint's room.
    const six = ['Open', 'Open with', 'Handler', 'Add to', 'Duplicate', 'Delete'].map(label => ({ id: label, label }));
    const wide = await mount(600, false, six);
    const tabs = box(wide.q('[role="group"]')).width;
    const sides = box(wide.region).width - box(row(wide)).width;
    await wide.view.unmount();
    const crowded = await mount(Math.ceil(tabs + sides + 30), false, six);
    const last = box(crowded.button('Delete'));
    expect(last.right).toBeLessThanOrEqual(box(row(crowded)).right + 1);
    // Wrapped below the row (which clips it), never clipped mid-word beside the tabs.
    expect(box(hint(crowded)).top).toBeGreaterThanOrEqual(box(row(crowded)).bottom - 1);
    await crowded.view.unmount();
});

it('in the Commands pane\'s 320px column six modes scroll sideways and keep the active one in view (CP-4)', async () => {
    const six = ['Open', 'Open with', 'Handler', 'Add to', 'Duplicate', 'Delete'].map(label => ({ id: label, label }));
    const column = (mode: string) => (
        <div data-testid="column" style={{ display: 'flex', flexDirection: 'column', width: 320, height: 600 }}>
            <CellPanel
                cellId="cell-1" kind="files" title="Files" host="pane"
                modes={six} mode={mode} onMode={() => {}}
                query="" onQuery={() => {}} placeholder="Find file"
                rows={[{ id: 'a', name: 'notes.md' }]} selectedId="a" onSelect={() => {}}
                onClose={() => {}}
            />
        </div>
    );
    const view = await render(column('Open'));
    const group = () => document.querySelector<HTMLElement>('[role="group"][aria-label="Mode"]')!;
    const tab = (name: string) => [...group().querySelectorAll('button')].find(b => b.textContent === name)!;
    expect(group().scrollWidth).toBeGreaterThan(group().clientWidth);
    expect(box(group()).right).toBeLessThanOrEqual(box(document.querySelector('[data-testid="column"]')!).right);
    await view.rerender(column('Delete'));
    expect(box(tab('Delete')).right).toBeLessThanOrEqual(box(group()).right + 0.75);
    expect(box(tab('Delete')).left).toBeGreaterThanOrEqual(box(group()).left - 0.75);
    await view.rerender(column('Open'));
    expect(box(tab('Open')).left).toBeGreaterThanOrEqual(box(group()).left - 0.75);
    await view.unmount();
});

/** A panel in the Commands pane's 340px column naming a new file. */
const naming = (query: string, completion?: string) => (
    <div data-testid="column" style={{ display: 'flex', flexDirection: 'column', width: 340, height: 600 }}>
        <CellPanel
            cellId="cell-1" kind="files" title="Files" host="pane"
            query={query} onQuery={() => {}} placeholder="Name the new file" completion={completion}
            primary={{ label: `Create ‘${query}’`, icon: Plus, intent: 'edit', onRun: () => {} }}
            hints={[{ keys: 'esc', label: 'cancel' }]}
            onClose={() => {}}
        />
    </div>
);

it('a footer hint without room is hidden whole, never cut mid-word', async () => {
    const view = await render(naming('a-really-long-name-for-a-new-file-that-fills-the-footer'));
    const hints = document.querySelector<HTMLElement>('[data-cell-panel-hints]')!;
    const esc = hints.firstElementChild!;
    expect(esc.textContent).toBe('esc cancel');
    // Wrapped below the line, which clips it.
    expect(box(esc).top).toBeGreaterThanOrEqual(box(hints).bottom - 1);
    await view.rerender(naming('a.md'));
    expect(box(esc).top).toBeGreaterThanOrEqual(box(hints).top - 1);
    expect(box(esc).right).toBeLessThanOrEqual(box(hints).right + 1);
    await view.unmount();
});

it('a completion keeps room at the field\'s end: a name that fills the field still shows it', async () => {
    const view = await render(naming('a-really-long-name-for-a-new-file-that-fills-the-field', '.md'));
    const input = document.querySelector<HTMLInputElement>('[role="combobox"]')!;
    input.scrollLeft = input.scrollWidth;
    await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
    expect(input.scrollLeft).toBeGreaterThan(0);
    const ghost = document.querySelector<HTMLElement>('[data-filename-completion]')!;
    const suffix = ghost.querySelector('.text-ink-3')!;
    expect(suffix.textContent).toBe('.md');
    expect(box(suffix).left).toBeGreaterThanOrEqual(box(ghost).left);
    expect(box(suffix).right).toBeLessThanOrEqual(box(ghost).right + 0.5);
    await view.unmount();
});
