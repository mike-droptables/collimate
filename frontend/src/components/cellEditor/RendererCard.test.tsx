import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { RendererCard } from './RendererCard';
import source from './RendererCard.tsx?raw';
import type { RendererEntry } from '@/lib/api';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

const renderer: RendererEntry = {
    id: 'r1', name: 'Notes', version: '1', summary: 'Writes notes.', extensions: ['.md', '.txt'], default_for: [],
    supports_diff: false, embed: 'native', content_hash: 'h', cell_id: 'cell', available: true,
};

function mount(over: Partial<RendererEntry> = {}, prefs: Record<string, string> = {}) {
    const spies = { onToggleAvailable: vi.fn(), onSetDefault: vi.fn(), onEditDisplay: vi.fn(), onOpenCell: vi.fn() };
    const view = render(<RendererCard renderer={{ ...renderer, ...over }} prefs={prefs} {...spies} />);
    const row = view.container.querySelector('[data-renderer-row="r1"]') as HTMLElement;
    const detail = () => row.querySelector('[data-renderer-detail]') as HTMLElement | null;
    const open = () => fireEvent.click(within(row).getByRole('button', { name: /Notes$/ }));
    return { ...spies, row, detail, open };
}

afterEach(cleanup);

describe('Renderer row (P04)', () => {
    it('is the mock row: a 12px 8px grid with the type, name and description, a line rule above (P04)', () => {
        const { row } = mount();
        expect(row.className).toMatch(/\bborder-t\b/);
        expect(row.className).toMatch(/\bborder-line\b/);
        const grid = row.firstElementChild as HTMLElement;
        expect(grid.className).toContain('grid-cols-[1fr_auto]');
        expect(grid.className).toContain('py-3');
        expect(grid.className).toContain('px-2');
        expect(within(row).getByText('.md .txt').className).toMatch(/\bfont-mono\b/);
        expect(within(row).getByText('Notes').className).toMatch(/\bfont-semibold\b/);
        expect(within(row).getByText('Notes').className).toMatch(/\btext-ink\b/);
        expect(within(row).getByText('Writes notes.').className).toContain('text-ink-2');
    });

    it('names the renderer id when it claims no extension (P04)', () => {
        const { row } = mount({ extensions: [] });
        expect(within(row).getByText('r1').className).toMatch(/\bfont-mono\b/);
    });

    it('toggles availability from the switch alone: the detail stays shut (P04)', () => {
        const { row, detail, onToggleAvailable } = mount();
        const toggle = within(row).getByRole('switch', { name: 'Notes available' });
        expect(toggle).toHaveAttribute('data-state', 'checked');
        fireEvent.click(toggle);
        expect(onToggleAvailable).toHaveBeenCalledWith(false);
        expect(detail()).toBeNull();
    });

    it('opens and shuts the detail from the row, with the expanded state on its button (P04)', () => {
        const { row, detail, open } = mount();
        const button = within(row).getByRole('button', { name: '.md .txt Notes' });
        expect(button).toHaveAttribute('aria-expanded', 'false');
        open();
        expect(detail()).not.toBeNull();
        expect(button).toHaveAttribute('aria-expanded', 'true');
        fireEvent.click(within(row).getByText('Writes notes.', { selector: 'p' }));
        expect(detail()).toBeNull();
    });

    it('carries the default-extension chips in the detail: the default is teal, a pick sets it (P04)', () => {
        const { detail, open, onSetDefault } = mount({}, { '.md': 'r1' });
        open();
        const chips = within(detail()!).getAllByRole('button', { name: /^Default renderer for/ });
        expect(chips.map(chip => chip.textContent)).toEqual(['.md', '.txt']);
        expect(chips[0]).toHaveAttribute('aria-pressed', 'true');
        expect(chips[0].className).toMatch(/\btext-teal\b/);
        expect(chips[0].className).toMatch(/\bbg-teal-soft\b/);
        expect(chips[0]).toBeDisabled();
        expect(chips[1].className).toMatch(/\bborder-line-2\b/);
        fireEvent.click(chips[1]);
        expect(onSetDefault).toHaveBeenCalledWith('.txt');
    });

    it('greys the chips of an unavailable renderer (P04)', () => {
        const { detail, open, onSetDefault } = mount({ available: false });
        open();
        const chip = within(detail()!).getByRole('button', { name: 'Default renderer for .md' });
        expect(chip).toBeDisabled();
        expect(chip.className).toMatch(/\btext-ink-3\b/);
        fireEvent.click(chip);
        expect(onSetDefault).not.toHaveBeenCalled();
    });

    it('edits the name and the summary through their labels in the detail (P04)', () => {
        const { detail, open, onEditDisplay } = mount();
        open();
        fireEvent.click(within(detail()!).getByText('Notes'));
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Jotter' } });
        fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
        expect(onEditDisplay).toHaveBeenCalledWith({ name: 'Jotter' });
        fireEvent.click(within(detail()!).getByText('Writes notes.'));
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keeps notes.' } });
        fireEvent.blur(screen.getByRole('textbox'));
        expect(onEditDisplay).toHaveBeenCalledWith({ summary: 'Keeps notes.' });
    });

    it('a built-in renderer: a Built-in label in the switch\'s place, its name and summary plain, its default chips kept', () => {
        const { row, detail, open, onSetDefault, onEditDisplay } = mount({ builtin: true });
        expect(within(row).queryByRole('switch')).toBeNull();
        expect(within(row).getByText('Built-in')).toHaveAttribute('title', 'Part of the app: always available, read-only');
        open();
        expect(within(detail()!).queryByText('Name')).toBeNull();
        expect(within(detail()!).queryByText('Summary')).toBeNull();
        expect(within(detail()!).queryByText('Writes notes.')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Default renderer for .md' }));
        expect(onSetDefault).toHaveBeenCalledWith('.md');
        expect(onEditDisplay).not.toHaveBeenCalled();
    });

    it('opens the cell that holds it from the detail: Open cell, or Open card for a built-in', () => {
        const user = mount();
        user.open();
        fireEvent.click(within(user.detail()!).getByRole('button', { name: 'Open cell' }));
        expect(user.onOpenCell).toHaveBeenCalledTimes(1);
        cleanup();
        const app = mount({ builtin: true });
        app.open();
        expect(within(app.detail()!).getByRole('button', { name: 'Open card' })).toBeInTheDocument();
        cleanup();
        render(<RendererCard renderer={renderer} prefs={{}} onToggleAvailable={vi.fn()} onSetDefault={vi.fn()} onEditDisplay={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: '.md .txt Notes' }));
        expect(screen.queryByRole('button', { name: 'Open cell' })).toBeNull();
    });

    it('paints only on the skin tokens (P04)', () => {
        expect(source).not.toMatch(RETIRED_TOKENS);
        expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(/);
    });
});
