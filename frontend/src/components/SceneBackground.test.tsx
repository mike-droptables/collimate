import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';

vi.mock('@/lib/api', () => ({
    putChannelSettings: vi.fn(() => Promise.resolve({})),
    fetchChannelSettings: vi.fn(),
}));

import { ThemeProvider, useThemeContext } from '@/contexts/ThemeContext';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { SCENE_CSS_VARS, type ThemeId } from '@/lib/themes';
import { SceneBackground } from './SceneBackground';

type Ctx = ReturnType<typeof useThemeContext>;

function Probe({ onCtx }: { onCtx: (ctx: Ctx) => void }) {
    onCtx(useThemeContext());
    return null;
}

let ctx!: Ctx;
let containerTop = 70;
let columnTop = 34;

/** The scene container in its column, under a top bar of
 *  containerTop - columnTop. */
function mount() {
    const view = render(
        <ThemeProvider>
            <Probe onCtx={value => { ctx = value; }} />
            <div data-testid="column"><div data-testid="container"><SceneBackground /></div></div>
        </ThemeProvider>,
    );
    return view;
}

const layer = () => document.querySelector('[data-scene-background]') as HTMLElement;
const choose = (id: ThemeId) => act(() => { ctx.setThemeId(id); });

describe('SceneBackground', () => {
    const rect = HTMLElement.prototype.getBoundingClientRect;
    beforeEach(() => {
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
        localStorage.clear();
        containerTop = 70;
        columnTop = 34;
        vi.stubGlobal('devicePixelRatio', 1.25);
        HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
            const top = this.dataset.testid === 'container' ? containerTop : this.dataset.testid === 'column' ? columnTop : 0;
            return { top, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
        };
    });
    afterEach(() => {
        cleanup();
        HTMLElement.prototype.getBoundingClientRect = rect;
        vi.unstubAllGlobals();
    });

    it('paints the scene ground from its host property, without a layer of its own when flat', () => {
        mount();
        choose('night');
        expect(layer().dataset.sceneBackground).toBe('none');
        expect(layer().style.backgroundColor).toBe(`var(${SCENE_CSS_VARS.ground})`);
        expect(layer().style.backgroundImage).toBe('');
        expect(layer()).not.toHaveClass('will-change-transform');
        expect(layer()).toHaveAttribute('aria-hidden', 'true');
        expect(layer()).toHaveClass('pointer-events-none');
    });

    it('a patterned scene is one promoted layer drawing every mark in the host properties\' colours', () => {
        mount();
        choose('grid');
        expect(layer().dataset.sceneBackground).toBe('grid');
        expect(layer()).toHaveClass('will-change-transform');
        expect(layer().children).toHaveLength(0);
        expect(layer().style.backgroundImage).toContain(`var(${SCENE_CSS_VARS.ink})`);
        expect(layer().style.backgroundImage).toContain(`var(${SCENE_CSS_VARS.inkFaint})`);
        expect(layer().style.backgroundImage).toContain(`var(${SCENE_CSS_VARS.glowWarm})`);
        expect(layer().style.backgroundSize).toContain('80px 80px');
    });

    it('starts on a whole device pixel: under a 70px bar at 1.25x it reaches up by under one', () => {
        mount();
        choose('grid');
        // 70 CSS px is 87.5 device px: half a device pixel, 0.4 CSS px, up.
        expect(layer().style.top).toBe('-0.40625px');  // 0.4 on layout's 1/64 grid
        expect(layer().style.left).toBe('0px');
    });

    it('reaches up through the top bar when the scene continues behind it, the column\'s top on a whole device pixel', () => {
        mount();
        choose('grid');
        expect(layer().style.top).toBe('-0.40625px');
        let id = '';
        act(() => { id = ctx.createUserTheme(); });
        act(() => { ctx.setUserThemeScene(id, 'behindTopBar', true); });
        // A 36px bar over a column at 34 CSS px, 42.5 device px at 1.25x:
        // the bar, then half a device pixel (0.4 CSS px) more.
        expect(layer().style.top).toBe('-36.40625px');
        expect(layer().style.left).toBe('0px');
        // Re-measured with the bar: a taller one on a resize.
        containerTop = 74;
        act(() => { window.dispatchEvent(new Event('resize')); });
        expect(layer().style.top).toBe('-40.40625px');
        // Off again: back under the bar by under a device pixel
        // (74 x 1.25 = 92.5 device px).
        act(() => { ctx.resetUserThemeScene(id, 'behindTopBar'); });
        expect(layer().style.top).toBe('-0.40625px');
    });

    it('re-snaps on a resize at a new ratio', () => {
        mount();
        choose('grid');
        containerTop = 72;
        vi.stubGlobal('devicePixelRatio', 1.1);
        act(() => { window.dispatchEvent(new Event('resize')); });
        // 72 × 1.1 = 79.2 device px: 0.2 device px, 0.18 CSS px, up; the pitch
        // (17.6 device px) snaps to four whole 4px rows, 16 device px.
        expect(layer().style.top).toBe('-0.1875px');
        expect(layer().style.backgroundSize).toContain(`${16 / 1.1 * 5}px`);
    });
});
