import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { BlurredTopBar } from './BlurredTopBar';
import { useTitleBarStore } from '@/stores/useTitleBarStore';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

const TOGGLE = 'Title bar';

// The channel-creation view replaces the AppTopBar with this blurred
// stand-in, so the AppTopBar's far-left title bar toggle must ride here too
// while the desktop title bar row is collapsed — otherwise the row can't be
// brought back from the create view at all.
describe('BlurredTopBar — the title bar toggle (desktop title bar collapsed)', () => {
    afterEach(() => {
        // Unmount before touching the store: the component subscribes to it,
        // and a reset with it still mounted is an update outside act.
        cleanup();
        uninstallDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: false });
        localStorage.removeItem('collimate.titleBarHidden');
    });

    it('browser tab: no icon even when the hidden flag is set', () => {
        useTitleBarStore.setState({ titleBarHidden: true });
        render(<BlurredTopBar />);
        expect(screen.queryByLabelText(TOGGLE)).toBeNull();
    });

    it('desktop with the title bar visible: no icon', () => {
        installDesktopShell();
        render(<BlurredTopBar />);
        expect(screen.queryByLabelText(TOGGLE)).toBeNull();
    });

    it('desktop + hidden: the icon shows above the blur and clicking it restores the title bar', () => {
        installDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: true });
        render(<BlurredTopBar />);

        const btn = screen.getByLabelText(TOGGLE);
        expect(btn.title).toBe('Show the title bar');
        expect(btn.getAttribute('aria-pressed')).toBe('false');
        // Live, not part of the inert blurred stand-in (aria-hidden,
        // pointer-events-none, filter: blur) — it must be clickable.
        expect(btn.closest('[aria-hidden]')).toBeNull();
        expect(btn.closest('.pointer-events-none')).toBeNull();

        fireEvent.click(btn);
        expect(useTitleBarStore.getState().titleBarHidden).toBe(false);
        // Icon leaves once the title bar is back.
        expect(screen.queryByLabelText(TOGGLE)).toBeNull();
    });
});

describe('BlurredTopBar — skin tokens (S04)', () => {
    afterEach(cleanup);
    it('the stand-in bar sits on the canvas with a hairline, its controls as sheets (S04)', () => {
        const { container } = render(<BlurredTopBar />);
        const bar = container.querySelector('[aria-hidden]') as HTMLElement;
        expect(bar.className.split(/\s+/)).toEqual(expect.arrayContaining(['bg-canvas', 'border-line']));
        expect(bar.querySelector('.bg-sheet-2')).not.toBeNull();
        expect(bar.outerHTML).not.toMatch(RETIRED_TOKENS);
    });
});

describe('BlurredTopBar — the 36px bar it stands in for', () => {
    afterEach(() => {
        cleanup();
        uninstallDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: false });
        localStorage.removeItem('collimate.titleBarHidden');
    });

    it('keeps the top bar\'s 36px row and three columns, and the restore icon in its slot (C01)', () => {
        installDesktopShell();
        useTitleBarStore.setState({ titleBarHidden: true });
        const { container } = render(<BlurredTopBar />);
        const bar = container.querySelector('[aria-hidden]') as HTMLElement;
        const classes = bar.className.split(/\s+/);
        expect(classes).toEqual(expect.arrayContaining(['h-9', 'grid', 'grid-cols-[1fr_auto_1fr]', 'px-2.5']));
        expect(classes).not.toContain('h-11');
        expect(bar.children).toHaveLength(3);
        const slot = screen.getByLabelText(TOGGLE).parentElement as HTMLElement;
        expect(slot.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-9', 'px-2.5']));
    });
});
