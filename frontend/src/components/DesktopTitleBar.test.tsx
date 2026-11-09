import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';
import { DesktopTitleBar, MarkGlyph, stripMnemonics } from './DesktopTitleBar';
import { installDesktopShell, uninstallDesktopShell, ALL_DESKTOP_CAPS } from '@/test/desktopShell';
import { useTitleBarStore } from '@/stores/useTitleBarStore';
import { retiredOr } from '@/test/retiredTokens';

afterEach(() => {
    cleanup();
    uninstallDesktopShell();
    useTitleBarStore.setState({ titleBarHidden: false });
    localStorage.removeItem('collimate.titleBarHidden');
});

describe('stripMnemonics', () => {
    it('drops Alt-mnemonic ampersands and keeps escaped ones', () => {
        expect(stripMnemonics('File')).toBe('File');
        expect(stripMnemonics('&File')).toBe('File');
        expect(stripMnemonics('Search && Replace')).toBe('Search & Replace');
        expect(stripMnemonics('A b &c')).toBe('A b c');
    });
});

describe('DesktopTitleBar', () => {
    it('renders nothing in a browser tab', () => {
        render(<DesktopTitleBar />);
        expect(screen.queryByTestId('desktop-title-bar')).toBeNull();
    });

    it('renders nothing when the shell keeps the native bar (macOS)', () => {
        installDesktopShell({ capabilities: { ...ALL_DESKTOP_CAPS, customTitleBar: false } });
        render(<DesktopTitleBar />);
        expect(screen.queryByTestId('desktop-title-bar')).toBeNull();
    });

    it('draws the menu entries the shell reports', async () => {
        installDesktopShell();
        render(<DesktopTitleBar />);
        expect(await screen.findByText('File')).toBeTruthy();
        expect(screen.getByText('Help')).toBeTruthy();
    });

    it('pops the clicked menu by its index, under the button', async () => {
        const { shell } = installDesktopShell();
        render(<DesktopTitleBar />);
        fireEvent.click(await screen.findByText('Help'));
        const popupMenu = shell.titleBar!.popupMenu as ReturnType<typeof vi.fn>;
        expect(popupMenu).toHaveBeenCalledTimes(1);
        const [index, x, y] = popupMenu.mock.calls[0];
        expect(index).toBe(1);
        expect(typeof x).toBe('number');
        expect(typeof y).toBe('number');
    });

    it('double-click on the bar toggles maximize; on a menu button it does not', async () => {
        const { shell } = installDesktopShell();
        render(<DesktopTitleBar />);
        const bar = await screen.findByTestId('desktop-title-bar');
        const toggleMaximize = shell.titleBar!.toggleMaximize as ReturnType<typeof vi.fn>;

        fireEvent.doubleClick(bar);
        expect(toggleMaximize).toHaveBeenCalledTimes(1);

        fireEvent.doubleClick(await screen.findByText('File'));
        expect(toggleMaximize).toHaveBeenCalledTimes(1);
    });

    it('draws its own window controls when the shell asks for them (frameless Linux)', async () => {
        const { shell } = installDesktopShell();
        render(<DesktopTitleBar />);
        await screen.findByTestId('desktop-title-bar');

        fireEvent.click(screen.getByLabelText('Minimize'));
        expect(shell.titleBar!.minimize).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByLabelText('Maximize'));
        expect(shell.titleBar!.toggleMaximize).toHaveBeenCalledTimes(1);
        // toggleMaximize resolved true → the button flips to Restore.
        expect(await screen.findByLabelText('Restore')).toBeTruthy();

        fireEvent.click(screen.getByLabelText('Close'));
        expect(shell.titleBar!.close).toHaveBeenCalledTimes(1);
    });

    it('leaves the controls to the shell overlay when customWindowControls is off (Windows)', async () => {
        installDesktopShell({
            capabilities: { ...ALL_DESKTOP_CAPS, customWindowControls: false },
        });
        render(<DesktopTitleBar />);
        await screen.findByTestId('desktop-title-bar');
        expect(screen.queryByLabelText('Minimize')).toBeNull();
        expect(screen.queryByLabelText('Close')).toBeNull();
    });

    it('the row carries no icon: the top bar\'s mark is its toggle, and the store bit hides and restores it (polish)', async () => {
        installDesktopShell();
        render(<DesktopTitleBar />);
        await screen.findByTestId('desktop-title-bar');
        expect(screen.queryByRole('button', { name: 'Title bar' })).toBeNull();
        expect(document.querySelector('img[src="/favicon.svg"]')).toBeNull();

        act(() => useTitleBarStore.getState().toggleTitleBar());
        expect(screen.queryByTestId('desktop-title-bar')).toBeNull();
        expect(useTitleBarStore.getState().titleBarHidden).toBe(true);
        expect(localStorage.getItem('collimate.titleBarHidden')).toBe('1');

        act(() => useTitleBarStore.getState().toggleTitleBar());
        expect(await screen.findByTestId('desktop-title-bar')).toBeTruthy();
    });

    it('paints the bar and its controls on the skin tokens, the same sheet the overlay takes (S05)', async () => {
        installDesktopShell();
        render(<DesktopTitleBar />);
        const bar = await screen.findByTestId('desktop-title-bar');
        expect(bar.className).toContain('bg-sheet');
        expect(bar.className).toContain('text-ink');
        expect(bar.className).toContain('border-line');
        expect((await screen.findByText('File')).className).toContain('hover:bg-sheet-2');

        const classes = [bar, ...Array.from(bar.querySelectorAll('*'))]
            .map(el => el.getAttribute('class') ?? '')
            .join(' ');
        expect(classes).not.toMatch(new RegExp(retiredOr('#[0-9a-f]{3,8}', 'rgba?\\(', '\\b(bg|text|border)-(white|black)\\b').source, 'i'));
    });
});

describe('MarkGlyph', () => {
    /** The rays' sub-paths as [x1, y1, x2, y2] segments. */
    const segments = (d: string) => d.split('M').filter(Boolean).map(part => part.trim().split(/\s+/).map(Number));
    /** Distance from point (px, py) to the segment (x1, y1)-(x2, y2). */
    const toSegment = ([x1, y1, x2, y2]: number[], px: number, py: number) => {
        const dx = x2 - x1, dy = y2 - y1;
        const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
        return Math.hypot(x1 + t * dx - px, y1 + t * dy - py);
    };

    it('draws a lens ring at the centre with the three rays split around it (polish)', () => {
        const { container } = render(<MarkGlyph />);
        const svg = container.querySelector('svg')!;
        const ring = svg.querySelector('circle')!;
        expect([ring.getAttribute('cx'), ring.getAttribute('cy'), ring.getAttribute('r')]).toEqual(['12', '12', '3.2']);
        // The ring takes the rays' stroke: no fill of its own, the svg's width.
        expect(svg.getAttribute('class')).toContain('fill-none');
        expect(ring.getAttribute('fill')).toBeNull();
        expect(ring.getAttribute('stroke-width')).toBeNull();
        const [rays, tip] = Array.from(svg.querySelectorAll('path'));
        const d = rays.getAttribute('d')!;
        expect(d).toMatchInlineSnapshot(`"M3 6 8.11 7.56M3 12 6.1 12M3 18 7.89 16.23"`);
        const segs = segments(d);
        // Each ray is one piece from the left edge, cut at the lens; the
        // tip on the right is a filled triangle, so it keeps its point at
        // every size.
        expect(segs).toHaveLength(3);
        expect(segs.map(([x1, y1]) => [x1, y1])).toEqual([[3, 6], [3, 12], [3, 18]]);
        expect(tip.getAttribute('d')).toBe('M17.3 9.8 21.6 12 17.3 14.2Z');
        expect(tip.getAttribute('fill')).toBe('currentColor');
        expect(tip.getAttribute('stroke')).toBe('none');
        // No ray stroke touches the ring's stroke: every piece stays more
        // than one stroke width (two half widths) plus a visible gap clear
        // of the ring's centre line.
        const stroke = Number(svg.getAttribute('stroke-width'));
        for (const seg of segs) expect(toSegment(seg, 12, 12)).toBeGreaterThan(3.2 + stroke + 0.5);
    });
});
