import React, { useEffect, useRef, useCallback, useMemo, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { Excalidraw, CaptureUpdateAction } from 'https://esm.sh/@excalidraw/excalidraw@0.18.1?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import {
    defineRenderer,
    mirrorUrl,
    resolveVar,
    StyleControl,
    toHex8,
    useCollimateFile,
    useStyle,
    useTheme,
} from '/api/sdk/react.js';

// Excalidraw loads its fonts (FontFace, which no import map routes) from
// window.EXCALIDRAW_ASSET_PATH, else from esm.sh: point it at the app's
// same-origin mirror of the release's dist/prod/, whose mirror_assets.json
// names the font files. Its stylesheet goes through api.loadCss, which the
// host maps the same way. Set before the first mount: Excalidraw reads it
// when it first registers its fonts.
export const EXCALIDRAW_ASSETS = 'https://cdn.jsdelivr.net/npm/@excalidraw/excalidraw@0.18.1/dist/prod/';
if (typeof window !== 'undefined') {
    (window as unknown as { EXCALIDRAW_ASSET_PATH?: string }).EXCALIDRAW_ASSET_PATH = mirrorUrl(EXCALIDRAW_ASSETS);
}

// ---------------------------------------------------------------------------
// The look: the skin, or Excalidraw's own
// ---------------------------------------------------------------------------
// The style control (top right: in Excalidraw's renderTopRightUI row while
// the drawing is editable, an overlay of its own while it is read-only) offers
// the skin (the default: the chrome on the skin's tokens and the drawing on
// the skin's canvas, below) or Excalidraw's own look: its own chrome and its
// own white ground, in the skin's scheme (its dark filter still follows it).
// Excalidraw's element fonts are its own, so the control has no font row.

/** The themes the style control offers; the first is the skin (null). */
export const EXCALIDRAW_THEMES = Object.freeze([
    Object.freeze({ id: 'skin', label: 'Collimate' }),
    Object.freeze({ id: 'excalidraw', label: 'Excalidraw' }),
]);

/** Whether a stored theme asks for Excalidraw's own look. */
export function ownLook(themeId: unknown): boolean {
    return themeId === 'excalidraw';
}

// ---------------------------------------------------------------------------
// The drawing's ground
// ---------------------------------------------------------------------------
// Excalidraw paints `viewBackgroundColor` into its canvas, and its dark mode
// is a CSS filter on that canvas (`invert(93%) hue-rotate(180deg)`), so a
// concrete skin colour handed to it in the dark scheme comes out inverted, and
// even a pre-inverted one cannot come out darker than the filter's floor (7%
// grey, what white becomes): night-cool's canvas, and any darker user skin's,
// is out of its reach. A drawing that follows the skin therefore gets a transparent ground:
// the canvas clears, the filter leaves clear pixels clear, and the mount's
// `var(--canvas)` under it shows through, exactly, in both schemes and across
// skin changes without touching the scene. A colour the user picked for the
// drawing is kept, painted by Excalidraw and saved as it is. Under
// Excalidraw's own look a drawing that follows the skin takes the library's
// own ground instead (its white, which its dark filter darkens).

/** Excalidraw's own default ground. A file carrying it, or no ground, follows the skin. */
export const EXCALIDRAW_DEFAULT_GROUND = '#ffffff';
/** The ground the renderer hands Excalidraw for a drawing that follows the skin. */
export const SKIN_GROUND = 'transparent';

/** Whether a file's `appState.viewBackgroundColor` leaves the ground to the skin. */
export function followsSkin(fileGround: unknown): boolean {
    if (fileGround === undefined || fileGround === null || fileGround === '') return true;
    return typeof fileGround === 'string' && fileGround.trim().toLowerCase() === EXCALIDRAW_DEFAULT_GROUND;
}

/** The `viewBackgroundColor` Excalidraw mounts with for a file's ground:
 *  under the skin (`own` false) a ground that follows it is SKIN_GROUND,
 *  under Excalidraw's own look the library's default. */
export function sceneGround(fileGround: unknown, own = false): string {
    if (!followsSkin(fileGround)) return String(fileGround);
    return own ? EXCALIDRAW_DEFAULT_GROUND : SKIN_GROUND;
}

/** The ground written back to the file: the file's own value while the
 *  drawing still follows the skin (on SKIN_GROUND, or on the library's
 *  default under its own look), else the colour the user chose. */
export function savedGround(stateGround: unknown, fileGround: unknown): unknown {
    return (stateGround === SKIN_GROUND || followsSkin(stateGround)) && followsSkin(fileGround)
        ? fileGround : stateGround;
}

// ---------------------------------------------------------------------------
// The chrome: Excalidraw's own CSS variables, set from the skin
// ---------------------------------------------------------------------------
// Excalidraw 0.18.1 declares its palette as variables on `.excalidraw`, and
// again on `.excalidraw.theme--dark`; every island, button, menu and dialog
// reads them. Each one the library sets to a colour (or a shadow, or its UI
// face) is mapped here to a skin token. `theme={scheme}` keeps the library's
// dark/light switch in step with the skin, so the two selectors carry the
// two schemes' maps; they differ only where the library's own ramp runs from
// light to dark (its greys and its lowest surface), which in a dark skin runs
// from the inks down to the canvas and in a light skin from the sheets up to
// the inks, and in the selection colour, which the dark scheme computes (see
// "The selection" below). `--theme-filter` stays the library's: it inverts
// the drawing (whose colours are stored for a light ground) in the dark scheme.

const tok = (name: string) => `var(${name})`;
const tint = (name: string, percent: number) => `color-mix(in srgb, var(${name}) ${percent}%, transparent)`;

const SHARED_VARS: Record<string, string> = {
    '--ui-font': tok('--font-ui'),
    // Islands, surfaces and their edges.
    '--island-bg-color': tok('--sheet-2'),
    '--default-bg-color': tok('--sheet'),
    '--popup-bg-color': tok('--sheet-2'),
    '--popup-secondary-bg-color': tok('--sheet-3'),
    '--popup-text-color': tok('--ink'),
    '--popup-text-inverted-color': tok('--sheet'),
    '--sidebar-bg-color': tok('--sheet-2'),
    '--sidebar-border-color': tok('--line-2'),
    '--dialog-border-color': tok('--line-2'),
    '--default-border-color': tok('--line-2'),
    '--avatar-border-color': tok('--line-2'),
    '--input-bg-color': tok('--sheet'),
    '--input-border-color': tok('--line-2'),
    '--input-hover-bg-color': tok('--sheet-2'),
    '--input-label-color': tok('--ink-2'),
    '--overlay-bg-color': tok('--lift'),
    '--color-surface-high': tok('--sheet-3'),
    '--color-surface-mid': tok('--sheet-2'),
    '--color-surface-low': tok('--sheet-2'),
    '--color-on-surface': tok('--ink'),
    '--color-border-outline': tok('--ink-3'),
    '--color-border-outline-variant': tok('--line-2'),
    '--shadow-island': tok('--shadow'),
    '--modal-shadow': tok('--shadow'),
    '--sidebar-shadow': tok('--shadow'),
    '--library-dropdown-shadow': tok('--shadow'),
    '--ImageExportModal-preview-border': tok('--line-2'),
    // Text and icons.
    '--text-primary-color': tok('--ink'),
    '--icon-fill-color': tok('--ink-2'),
    '--keybinding-color': tok('--ink-3'),
    '--color-disabled': tok('--ink-3'),
    '--color-logo-text': tok('--ink'),
    '--color-icon-white': tok('--sheet'),
    // Buttons, sliders, scrollbars.
    '--button-hover-bg': tok('--sheet-3'),
    '--button-active-bg': tok('--sheet-3'),
    '--button-active-border': tok('--brass'),
    '--button-gray-1': tok('--sheet-2'),
    '--button-gray-2': tok('--sheet-3'),
    '--button-gray-3': tok('--line-2'),
    '--button-special-active-bg-color': tok('--teal-soft'),
    '--button-destructive-bg-color': tok('--red-soft'),
    '--button-destructive-color': tok('--red'),
    '--color-slider-track': tok('--sheet-3'),
    '--color-slider-thumb': tok('--ink-2'),
    '--scrollbar-thumb': tok('--line-2'),
    '--scrollbar-thumb-hover': tok('--ink-3'),
    // The selection accent: the selected tool, the active choice.
    '--color-primary': tok('--brass'),
    '--color-primary-darker': tok('--brass'),
    '--color-primary-darkest': tok('--brass'),
    '--color-primary-hover': tok('--brass'),
    '--color-primary-light': tok('--brass-soft'),
    '--color-primary-light-darker': tok('--brass-soft'),
    '--color-brand-hover': tok('--brass'),
    '--color-brand-active': tok('--brass'),
    '--color-on-primary-container': tok('--brass'),
    '--color-surface-primary-container': tok('--brass-soft'),
    // Live state: links and focus.
    '--link-color': tok('--teal'),
    '--focus-highlight-color': tok('--teal'),
    '--select-highlight-color': tok('--teal'),
    // Status: warning on brass, danger on red, success on green.
    '--color-warning': tok('--brass-soft'),
    '--color-warning-dark': tok('--brass'),
    '--color-warning-darker': tok('--brass'),
    '--color-warning-darkest': tok('--brass'),
    '--color-text-warning': tok('--ink'),
    '--color-warning-color': tok('--ink'),
    '--color-warning-icon-color': tok('--ink'),
    '--color-danger': tok('--red'),
    '--color-danger-dark': tok('--red'),
    '--color-danger-darker': tok('--red'),
    '--color-danger-darkest': tok('--red'),
    '--color-danger-text': tok('--red'),
    '--color-danger-background': tok('--red-soft'),
    '--color-danger-icon-background': tok('--red-soft'),
    '--color-danger-color': tok('--red'),
    '--color-danger-icon-color': tok('--red'),
    '--color-success': tint('--green', 16),
    '--color-success-darker': tint('--green', 24),
    '--color-success-darkest': tint('--green', 32),
    '--color-success-text': tok('--green'),
    '--color-success-contrast': tok('--green'),
    '--color-success-contrast-hover': tok('--green'),
    '--color-success-contrast-active': tok('--green'),
    '--icon-green-fill-color': tok('--green'),
    '--color-badge': tok('--green'),
    '--background-color-badge': tint('--green', 16),
};

// The library's grey ramp, lightest (10) to darkest (100), and its lowest
// surface. The light scheme also carries the selection outline: its canvas is
// unfiltered, so the outline is the skin's brass as it is.
const LIGHT_RAMP: Record<string, string> = {
    '--color-selection': tok('--brass'),
    '--color-surface-lowest': tok('--sheet'),
    '--color-gray-10': tok('--sheet-2'),
    '--color-gray-20': tok('--sheet-3'),
    '--color-gray-30': tok('--line'),
    '--color-gray-40': tok('--line-2'),
    '--color-gray-50': tok('--ink-3'),
    '--color-gray-60': tok('--ink-3'),
    '--color-gray-70': tok('--ink-2'),
    '--color-gray-80': tok('--ink-2'),
    '--color-gray-85': tok('--ink'),
    '--color-gray-90': tok('--ink'),
    '--color-gray-100': tok('--ink'),
};

const DARK_RAMP: Record<string, string> = {
    '--color-surface-lowest': tok('--canvas'),
    '--color-gray-10': tok('--ink'),
    '--color-gray-20': tok('--ink'),
    '--color-gray-30': tok('--ink-2'),
    '--color-gray-40': tok('--ink-2'),
    '--color-gray-50': tok('--ink-3'),
    '--color-gray-60': tok('--ink-3'),
    '--color-gray-70': tok('--line-2'),
    '--color-gray-80': tok('--line'),
    '--color-gray-85': tok('--sheet-3'),
    '--color-gray-90': tok('--sheet-2'),
    '--color-gray-100': tok('--canvas'),
};

/** Excalidraw's variables for one scheme, each a skin token (or a tint of one).
 *  The dark scheme's `--color-selection` is not among them: see darkSelectionCss. */
export function excalidrawVars(scheme: 'light' | 'dark'): Record<string, string> {
    return { ...SHARED_VARS, ...(scheme === 'dark' ? DARK_RAMP : LIGHT_RAMP) };
}

const declarations = (vars: Record<string, string>) =>
    Object.entries(vars).map(([name, value]) => `${name}: ${value};`).join(' ');

/** The skin's stylesheet, rendered into the frame while the skin is the
 *  look (a React <style>, so Excalidraw's own look removes it). `html` in
 *  front of each selector
 *  outranks the library's `.excalidraw` and `.excalidraw.theme--dark` rules
 *  whatever order the two sheets load in. Values read the skin variables, so a
 *  skin change repaints the chrome without rebuilding the sheet. Every library
 *  rule that paints a literal colour rather than a variable, on a component
 *  0.18.1 renders, is restated here on the same selector and the same tokens
 *  (theme.test.ts checks this against the library's own stylesheet). What is
 *  left painting the library's colours: the collaboration UI, which this
 *  renderer never turns on; the library-publishing form, which submits to
 *  excalidraw.com's public library; and the error splash, which this build of
 *  the package never renders. */
export function excalidrawCss(): string {
    return [
        'html, body, #mount { width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; background: var(--canvas); }',
        `html .excalidraw { ${declarations(excalidrawVars('light'))} }`,
        `html .excalidraw.theme--dark { ${declarations(excalidrawVars('dark'))} }`,
        'html .excalidraw-tooltip { --ui-font: var(--font-ui); background: var(--ink); color: var(--sheet); }',
        'html .excalidraw .Modal__background { background-color: var(--lift); }',
        'html .excalidraw .context-menu { box-shadow: var(--shadow); }',
        'html .excalidraw .context-menu-item:hover { color: var(--ink); background-color: var(--sheet-3); }',
        'html .excalidraw .context-menu-item-separator { border-top-color: var(--line-2); }',
        'html .excalidraw .context-menu-item.dangerous .context-menu-item__label { color: var(--red); }',
        'html .excalidraw .context-menu-item:hover.dangerous { background-color: var(--red-soft); }',
        'html .excalidraw .context-menu-item:hover.dangerous .context-menu-item__label { color: var(--red); }',
        'html .excalidraw-hyperlinkContainer { box-shadow: var(--shadow); }',
        'html .excalidraw-hyperlinkContainer button { color: var(--teal); }',
        'html .excalidraw-hyperlinkContainer button.excalidraw-hyperlinkContainer--remove, html .excalidraw-hyperlinkContainer--remove .ToolIcon__icon svg { color: var(--red); }',
        'html .excalidraw .ElementLinkDialog .ElementLinkDialog__input .ElementLinkDialog__remove, html .excalidraw .ElementLinkDialog .ElementLinkDialog__input .ElementLinkDialog__remove .ToolIcon__icon svg { color: var(--red); }',
        'html .excalidraw .Checkbox .Checkbox-box { color: var(--brass); box-shadow: 0 0 0 2px var(--line-2); }',
        'html .excalidraw .Checkbox:hover .Checkbox-box { background-color: var(--sheet-3); }',
        'html .excalidraw .Checkbox:hover:not(.is-checked) .Checkbox-box:not(:focus) { box-shadow: 0 0 0 2px var(--ink-3); }',
        'html .excalidraw .Checkbox.is-checked .Checkbox-box, html .excalidraw .Checkbox.is-checked:hover .Checkbox-box { background-color: var(--brass-soft); }',
        'html .excalidraw .Checkbox .Checkbox-box:focus { box-shadow: 0 0 0 3px var(--teal); }',
        'html .excalidraw .Checkbox:active .Checkbox-box { box-shadow: 0 0 2px 1px inset var(--brass) !important; }',
        'html .excalidraw .picker { border-color: var(--line-2); }',
        'html .excalidraw .excalidraw-canvas-buttons { box-shadow: var(--shadow); }',
        // Text on a brass or red fill: the sheet, light on paper's dark accents
        // and dark on a dark skin's light ones. The library's dark rules for the
        // dialog buttons, the library button and the text-to-diagram button
        // already read its ramp (here the canvas).
        'html .excalidraw .Dialog__action-button--primary, html .excalidraw .Dialog__action-button--danger, html .excalidraw .library-menu-browse-button, html .excalidraw .ttd-dialog-panel-button.excalidraw-button { color: var(--sheet); }',
        'html .excalidraw .ttd-dialog-panel-button div.Spinner { --spinner-color: var(--sheet); }',
        'html .excalidraw .ExcButton--color-warning.ExcButton--variant-filled { --text-color: var(--sheet); }',
        // The export dialog's cards set the open-color lime and pink inline,
        // which only an !important declaration outranks.
        'html .excalidraw .Card { --card-color: var(--brass) !important; --card-color-darker: var(--brass) !important; --card-color-darkest: var(--brass) !important; }',
        'html .excalidraw .Card .Card-icon, html .excalidraw .Card .Card-button.ToolIcon_type_button .ToolIcon__label { color: var(--sheet); }',
        'html .excalidraw .Card .Card-button.ToolIcon_type_button .Spinner { --spinner-color: var(--sheet); }',
        'html .excalidraw .PasteChartDialog .ChartPreview { border-color: var(--line-2); }',
        'html .excalidraw .PasteChartDialog .ChartPreview:hover { border-color: var(--brass); }',
        'html .excalidraw__embeddable-container .excalidraw__embeddable-hint { background: var(--sheet-2); color: var(--ink); box-shadow: var(--shadow); }',
        // The dark theme's hover fills are literals at the library's own
        // doubled-class specificity; these restate them one step higher.
        'html .excalidraw.theme--dark.excalidraw .App-menu__left, html .excalidraw.theme--dark.excalidraw .App-mobile-menu { --button-hover-bg: var(--sheet-3); }',
        // The menu and help buttons paint their hover from a variable the
        // dark theme sets to a literal; these paint the fill (and the help
        // button's edge, which reads the same variable) directly.
        'html .excalidraw.theme--dark.excalidraw .dropdown-menu-button:hover { background-color: var(--sheet-3); }',
        'html .excalidraw.theme--dark.excalidraw .HelpDialog__btn:hover { background-color: var(--sheet-3); border-color: var(--sheet-3); }',
        'html .excalidraw .layer-ui__search-header .ExcTextField__input, html .excalidraw.theme--dark.excalidraw .layer-ui__search-header .ExcTextField__input { background-color: var(--sheet); }',
        'html .excalidraw .App-toolbar .eraser.ToolIcon:hover { --icon-fill-color: var(--ink); --keybinding-color: var(--ink-2); }',
        'html .LoadingMessage, html .LoadingMessage--dark { background-color: var(--canvas); color: var(--ink-2); }',
    ].join('\n');
}

// ---------------------------------------------------------------------------
// The selection
// ---------------------------------------------------------------------------
// Excalidraw draws the selection box and its handles on its interactive
// canvas, in the colour it reads from the container's `--color-selection` on
// every interactive render. In the dark scheme that canvas carries the same
// filter as the drawing, `invert(93%) hue-rotate(180deg)`, so the colour has
// to be handed over pre-inverted (the library's own dark value is exactly
// that). The filter, per sRGB channel, is out = H·(0.93 − 0.86·in),
// where H is the hue-rotate(180deg) matrix of Filter Effects 1; H is its own
// inverse, so in = (0.93 − H·out) / 0.86. The filter cannot produce every
// colour: a brass that would need a channel outside 0–1 (a very bright or
// saturated one) has that channel clamped, and the outline comes out as close
// to it as the filter allows. The shipped skins' brasses are all in reach.

const HUE_ROTATE_180 = [
    [-0.574, 1.43, 0.144],
    [0.426, 0.43, 0.144],
    [0.426, 1.43, -0.856],
];

const hex2 = (unit: number) =>
    Math.round(Math.min(1, Math.max(0, unit)) * 255).toString(16).padStart(2, '0');

/** The colour Excalidraw's dark filter turns into `color`, as `#rrggbb`
 *  (`#rrggbbaa` when the colour is translucent), or null when it is not a
 *  colour toHex8 reads. */
export function preInverted(color: string): string | null {
    const hex = toHex8(color);
    if (!hex) return null;
    const out = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const input = HUE_ROTATE_180.map((row) =>
        (0.93 - (row[0] * out[0] + row[1] * out[1] + row[2] * out[2])) / 0.86);
    const alpha = hex.slice(7);
    return '#' + input.map(hex2).join('') + (alpha === 'ff' ? '' : alpha);
}

/** The dark scheme's selection colour, from the skin record: the skin's brass,
 *  pre-inverted for the filter. Empty until the record names a brass. */
export function darkSelectionCss(theme: { vars?: Record<string, string> }): string {
    const color = preInverted(resolveVar(theme, '--brass'));
    return color ? `html .excalidraw.theme--dark { --color-selection: ${color}; }` : '';
}

/** Where the style control sits: in Excalidraw's top-right row, in flow. */
const STYLE_CONTROL_PLACE = Object.freeze({ position: 'relative', top: 'auto', right: 'auto', alignSelf: 'center' });

/** Where the style control sits on a read-only drawing: over the canvas, top
 *  right, above Excalidraw's own layers. Excalidraw's mobile layout (the one
 *  a cell-sized drawing gets) leaves out the row renderTopRightUI fills
 *  whenever view mode is on, so a read-only drawing mounts the control here. */
const STYLE_CONTROL_OVERLAY = Object.freeze({ position: 'absolute', top: 8, right: 8, zIndex: 10 });

function ExcalidrawWrapper() {
    const { content, saveContent, isReadOnly, api } = useCollimateFile();
    const theme = useTheme(api);
    const { scheme } = theme;
    const style = useStyle(api);
    const own = ownLook(style.theme);
    const ownRef = useRef(own);
    ownRef.current = own;
    // The drawing's wrapper: the style control in Excalidraw's top-right
    // row measures the pointer over all of it, not over that small row.
    const wrapRef = useRef<HTMLDivElement | null>(null);
    // The skin's sheet and its dark selection colour, both gone under
    // Excalidraw's own look.
    const skinCss = useMemo(() => excalidrawCss(), []);
    const selectionCss = own ? '' : darkSelectionCss(theme);
    const [excalidraw, keepExcalidraw] = useState(null);
    const applyingHostRef = useRef(false);
    const contentRef = useRef(content);
    contentRef.current = content;
    const saveRef = useRef(saveContent);
    saveRef.current = saveContent;
    const sceneRef = useRef(null);
    const baselineRef = useRef(null);

    useEffect(() => {
        let cancelled = false;
        // Wait for the excalidraw stylesheet before signalling ready —
        // otherwise the canvas paints with default (light) styles for a flash.
        api.loadCss('https://cdn.jsdelivr.net/npm/@excalidraw/excalidraw@0.18.1/dist/prod/index.css')
            .then(() => {
                if (cancelled) return;
                requestAnimationFrame(() => { if (!cancelled && api.ready) api.ready(); });
            });
        return () => { cancelled = true; };
    }, [api]);

    // Excalidraw reads initialData only at mount. Keep the file's own appState
    // (its persisted theme and ground) apart from what the canvas is handed:
    // a ground that follows the skin is mounted as SKIN_GROUND (or the
    // library's own under its own look) and written back as the file had it.
    const { initialData, fileAppState } = useMemo(() => {
        let file = { elements: [], appState: {}, files: {} };
        const c = contentRef.current;
        if (c) {
            try {
                const parsed = JSON.parse(c);
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) file = parsed;
            } catch (e) { /* an unreadable file opens empty */ }
        }
        const appState = file.appState ?? {};
        return {
            fileAppState: appState,
            initialData: { ...file, appState: { ...appState, viewBackgroundColor: sceneGround(appState.viewBackgroundColor, ownRef.current) } },
        };
    }, []);

    const fileStateRef = useRef(fileAppState);
    const serialize = useCallback((s) => JSON.stringify({
        type: 'excalidraw', version: 2, source: 'collimate',
        elements: s.els,
        appState: {
            theme: fileStateRef.current.theme,
            viewBackgroundColor: savedGround(s.state.viewBackgroundColor, fileStateRef.current.viewBackgroundColor),
        },
        files: s.files,
    }, null, 2), []);

    // Publish document changes synchronously; selection/pan echoes dedupe.
    const flushScene = useCallback(() => {
        const scene = sceneRef.current;
        if (!scene) return;
        sceneRef.current = null;
        const next = serialize(scene);
        if (next === baselineRef.current || next === contentRef.current) return;
        baselineRef.current = next;
        saveRef.current(next);
    }, [serialize]);
    useEffect(() => api.onFlush?.(flushScene), [api, flushScene]);

    // Apply host-selected revisions without remounting the drawing or moving
    // its viewport. Remote updates stay outside the user's undo history.
    useEffect(() => {
        if (!excalidraw || content === baselineRef.current) return;
        const file = JSON.parse(content || '{"elements":[],"appState":{},"files":{}}');
        fileStateRef.current = file.appState ?? {};
        const scene = { els: file.elements ?? [], state: fileStateRef.current, files: file.files ?? {} };
        baselineRef.current = serialize(scene);
        sceneRef.current = null;
        applyingHostRef.current = true;
        try {
            excalidraw.addFiles(Object.values(scene.files));
            excalidraw.updateScene({ elements: scene.els,
                appState: { viewBackgroundColor: sceneGround(scene.state.viewBackgroundColor, ownRef.current) },
                captureUpdate: CaptureUpdateAction.NEVER });
        } finally { applyingHostRef.current = false; }
    }, [content, excalidraw, serialize]);

    // The look changed (or the host's style arrived after the mount): a
    // drawing that follows the skin moves to that look's ground; a colour the
    // user chose stays. Out of undo history, and never saved: savedGround
    // writes the file's own ground back.
    useEffect(() => {
        if (!excalidraw) return;
        const ground = excalidraw.getAppState().viewBackgroundColor;
        if (ground !== SKIN_GROUND && !followsSkin(ground)) return;
        const next = own ? EXCALIDRAW_DEFAULT_GROUND : SKIN_GROUND;
        if (ground === next) return;
        applyingHostRef.current = true;
        try {
            excalidraw.updateScene({ appState: { viewBackgroundColor: next }, captureUpdate: CaptureUpdateAction.NEVER });
        } finally { applyingHostRef.current = false; }
    }, [own, excalidraw]);

    // A skin change within one scheme leaves Excalidraw's props as they were,
    // so its interactive canvas would keep the old selection colour until the
    // next pointer move. A fresh copy of the selection makes it redraw, now
    // reading the new `--color-selection`.
    useEffect(() => {
        if (!excalidraw) return;
        excalidraw.updateScene({ appState: { selectedElementIds: { ...excalidraw.getAppState().selectedElementIds } } });
    }, [selectionCss, excalidraw]);

    const handleChange = useCallback((els, state, files) => {
        if (isReadOnly || applyingHostRef.current) return;
        const scene = { els, state, files };
        if (baselineRef.current === null) {
            baselineRef.current = serialize(scene);
            return;
        }
        sceneRef.current = scene;
        flushScene();
    }, [isReadOnly, flushScene, serialize]);

    // The style control, shown while the pointer is near it: in
    // Excalidraw's own top-right row while the drawing is editable (watching
    // the wrapper, since its parent is only that row), over the canvas while
    // it is read-only (see STYLE_CONTROL_OVERLAY; its parent is the wrapper).
    const renderTopRightUI = useCallback(() => (
        <StyleControl api={api} themes={EXCALIDRAW_THEMES} font={false} size={false}
            watch={wrapRef} style={STYLE_CONTROL_PLACE} />
    ), [api]);

    // Excalidraw knows only light and dark, so `theme` follows the skin's
    // scheme under either look (its dark filter inverts the drawing). Under
    // the skin the chrome's colours come from the skin's sheet, the ground
    // from the mount and the dark selection colour from the style element,
    // rebuilt on every skin change (see above); under Excalidraw's own look
    // neither sheet is rendered.
    return (
        <div ref={wrapRef} style={{ position: 'relative', width: '100%', height: '100%' }}>
            {!own && <style>{skinCss}</style>}
            <style>{selectionCss}</style>
            <Excalidraw
                excalidrawAPI={keepExcalidraw}
                theme={scheme}
                initialData={initialData}
                onChange={!isReadOnly ? handleChange : undefined}
                viewModeEnabled={isReadOnly}
                renderTopRightUI={isReadOnly ? undefined : renderTopRightUI}
            />
            {isReadOnly && (
                <StyleControl api={api} themes={EXCALIDRAW_THEMES} font={false} size={false}
                    style={STYLE_CONTROL_OVERLAY} />
            )}
        </div>
    );
}

// deferReady: excalidraw ships its own stylesheet; auto-ready would lift
// the overlay before that stylesheet lands.
export default defineRenderer({ component: ExcalidrawWrapper, deferReady: true });
