import { useLayoutEffect, useRef, useState } from 'react';
import { useThemeContext } from '@/contexts/ThemeContext';
import { SCENE_CSS_VARS } from '@/lib/themes';
import { sceneBackground, sceneGeometry, scenePaint } from '@/lib/sceneLayers';
import { deviceFloorOffset, watchDevicePixels } from '@/lib/deviceOrigin';
import { cn } from '@/lib/utils';

/** The colours, as the host properties ThemeContext writes on <html>. */
const PAINT = scenePaint(name => `var(${name})`);

interface DeviceGrid {
    dpr: number;
    /** CSS px the layer reaches above and left of its container: under one
     *  device pixel to start on a whole one, and up through the top bar
     *  when the scene continues behind it. */
    top: number;
    left: number;
}

/**
 * The ground behind the stacks: the active skin's scene (lib/themes
 * ThemeScene), viewport-fixed under the scene layer, never moved by the
 * camera.
 *
 * A patterned scene is exactly ONE compositor layer: this element is
 * promoted and holds every mark as one of its own backgrounds, so the whole
 * ground is rasterised once. Its origin sits on a whole device pixel (it
 * reaches up and left under the top bar by less than one) and every pitch
 * is a whole number of device pixels at the current ratio, re-snapped on a
 * zoom or a move to another display, so no line beats. A flat scene is a
 * plain fill and takes no layer.
 *
 * When the scene continues behind the top bar (ThemeScene.behindTopBar) the
 * layer reaches up by the bar's height too, measured as the distance from
 * the scene container's top to its column's, so the pattern runs on
 * unbroken behind the bar's transparent fill; the bar (z-30 in the column)
 * paints over it. The origin is then the column's top, floored to a device
 * pixel the same way.
 */
export function SceneBackground() {
    const { scene } = useThemeContext();
    const behindTopBar = scene.behindTopBar;
    const ref = useRef<HTMLDivElement>(null);
    const [grid, setGrid] = useState<DeviceGrid>({ dpr: 1, top: 0, left: 0 });

    useLayoutEffect(() => {
        const el = ref.current;
        const container = el?.parentElement;
        if (!el || !container) return undefined;
        return watchDevicePixels(el, () => {
            const dpr = window.devicePixelRatio || 1;
            const r = container.getBoundingClientRect();
            const column = container.parentElement;
            const bar = behindTopBar && column ? Math.max(0, r.top - column.getBoundingClientRect().top) : 0;
            const top = bar + deviceFloorOffset(r.top - bar, dpr);
            const next = { dpr, top, left: deviceFloorOffset(r.left, dpr) };
            setGrid(prev => (prev.dpr === next.dpr && prev.top === next.top && prev.left === next.left ? prev : next));
        });
    }, [behindTopBar]);

    const layers = sceneBackground(scene, PAINT, sceneGeometry(scene.pitch, grid.dpr));
    return (
        <div
            ref={ref}
            aria-hidden="true"
            data-scene-background={scene.pattern}
            className={cn('pointer-events-none absolute right-0 bottom-0', layers && 'will-change-transform')}
            style={{
                top: -grid.top,
                left: -grid.left,
                backgroundColor: `var(${SCENE_CSS_VARS.ground})`,
                ...layers,
            }}
        />
    );
}
