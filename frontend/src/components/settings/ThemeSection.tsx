import { useId, useRef, useState } from 'react';
import { Plus, RotateCcw } from 'lucide-react';
import { useThemeChannel, useThemeContext } from '@/contexts/ThemeContext';
import {
    SCENE_MAJOR, SCENE_PATTERNS, SCENE_PITCHES, SELECTION_EDGES, SceneGlow, ScenePattern, ThemeDef,
    ThemeScene, ThemeVarKey, THEMES, getTheme, isBuiltinThemeId, sceneCssVars, sceneGround, softVariant,
} from '@/lib/themes';
import { FONT_CATALOGUE, FontRole, familyForStack } from '@/lib/fonts';
import {
    UserTheme, FONT_ROLE_VARS, SOFT_ACCENTS, isSceneColor, isSoftAccent, hexOf, userThemeToDef,
} from '@/lib/userThemes';
import { sceneBackground, sceneGeometry, scenePaint } from '@/lib/sceneLayers';
import { blobUrl } from '@/lib/api/history';
import { streamBlob } from '@/lib/api/transfer';
import {
    btn, btnDanger, card, cardHeader, confirmStrip, formInput, formLabel, formSelect, iconBtn,
    panelLabel, sectionLabel, segmentOff, segmentOn,
} from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { Switch } from './ToggleRow';

// ─── Theme section ───────────────────────────────────────────────────────────
// One column in the Commands pane's settings view: the channel's look (the
// interface size is this device's, in Settings → This device). The theme
// cards two to a row: the three built-ins,
// the channel's user themes, then "+", which makes a user theme from the
// active one. Clicking a card applies it. Built-ins are read-only; the editor
// opens under the cards for the active user theme: its name, base and
// scheme stacked, the token pickers by group, the background (the scene:
// pattern, pitch, strength, ground and ink, glows and vignette), the radius,
// the selection edge and the three font roles. Shadow and lift always come
// from the base.

interface TokenSpec { key: ThemeVarKey; label: string }

/** The colour pickers by group: every skin token except shadow, lift, the
 *  radius and the font stacks, which have controls of their own or none. */
const TOKEN_GROUPS: { title: string; hint?: string; tokens: TokenSpec[] }[] = [
    {
        title: 'Surfaces',
        tokens: [
            { key: 'canvas', label: 'Canvas' },
            { key: 'sheet', label: 'Sheet' },
            { key: 'sheet2', label: 'Sheet 2' },
            { key: 'sheet3', label: 'Sheet 3' },
            { key: 'tealSheet', label: 'Teal sheet' },
        ],
    },
    {
        title: 'Lines',
        tokens: [
            { key: 'line', label: 'Line' },
            { key: 'line2', label: 'Line 2' },
        ],
    },
    {
        title: 'Inks',
        tokens: [
            { key: 'ink', label: 'Ink' },
            { key: 'ink2', label: 'Ink 2' },
            { key: 'ink3', label: 'Ink 3' },
            { key: 'proseInk', label: 'Prose ink' },
        ],
    },
    {
        title: 'Accents',
        hint: 'Brass marks the selection, teal what is live, red what destroys, green what passed. A soft fill follows its accent until you set it.',
        tokens: [
            { key: 'brass', label: 'Brass' },
            { key: 'brassSoft', label: 'Brass soft' },
            { key: 'teal', label: 'Teal' },
            { key: 'tealSoft', label: 'Teal soft' },
            { key: 'red', label: 'Red' },
            { key: 'redSoft', label: 'Red soft' },
            { key: 'green', label: 'Green' },
        ],
    },
    {
        title: 'Code',
        tokens: [{ key: 'codeBg', label: 'Code ground' }],
    },
];

const RADIUS_OPTIONS = ['2px', '4px', '6px', '8px', '10px', '12px'];

const PATTERN_LABELS: Record<ScenePattern, string> = {
    none: 'None', grid: 'Grid', dots: 'Dots', 'fine-dots': 'Fine dots', scanlines: 'Scanlines',
};

const GLOWS = [
    { key: 'glowWarm', label: 'Warm glow' },
    { key: 'glowCool', label: 'Cool glow' },
] as const;

/** A switched-on glow's alpha when its base has none to start from. */
const GLOW_START_ALPHA = 0.2;

const FONT_ROWS: { role: FontRole; label: string; sample: string }[] = [
    { role: 'ui', label: 'Interface', sample: 'Run in a new cell' },
    { role: 'prose', label: 'Prose', sample: 'The quick brown fox' },
    { role: 'mono', label: 'Code', sample: 'print(0x1f)' },
];

/** Whether `value` is a colour the browser paints. */
function isColor(value: string): boolean {
    if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function') return CSS.supports('color', value);
    return hexOf(value) !== undefined;
}

/** A small preview of a skin painted in its own tokens: a sheet on its
 *  scene (the ground and its pattern, glows and vignette at full size), two
 *  ink lines, a selected row in brass and a live dot in teal. */
function ThemePreview({ theme }: { theme: ThemeDef }) {
    const { vars, scene } = theme;
    const channel = useThemeChannel();
    const values = sceneCssVars(scene, vars, channel);
    const layers = sceneBackground(scene, scenePaint(name => values[name]), sceneGeometry(scene.pitch, 1));
    return (
        <div
            aria-hidden
            data-scene-preview={scene.pattern}
            className="h-14 overflow-hidden rounded-md p-1.5"
            style={{ backgroundColor: sceneGround(scene, vars), ...layers, border: `1px solid ${vars.line}` }}
        >
            <div
                className="flex h-full flex-col justify-center gap-1 rounded-sm px-2"
                style={{ background: vars.sheet, border: `1px solid ${vars.line}` }}
            >
                <div className="flex items-center gap-1.5">
                    <span className="size-1.5 flex-none rounded-full" style={{ background: vars.teal }} />
                    <span className="h-1 w-2/5 rounded-full" style={{ background: vars.ink }} />
                </div>
                <span className="h-1 w-3/5 rounded-full" style={{ background: vars.ink3 }} />
                <span
                    className="h-2 w-full rounded-xs"
                    style={{ background: vars.brassSoft, boxShadow: `inset 2px 0 0 ${vars.brass}` }}
                />
            </div>
        </div>
    );
}

function ThemeCard({ theme, detail, selected, onSelect }: {
    theme: ThemeDef; detail: string; selected: boolean; onSelect: () => void;
}) {
    const label = theme.label || 'Untitled';
    return (
        <button
            type="button"
            aria-pressed={selected}
            aria-label={label}
            onClick={onSelect}
            className={cn(
                'flex min-w-0 flex-col gap-2 rounded-lg border p-2 text-left transition-colors',
                selected ? 'border-brass bg-brass-soft' : 'border-line bg-sheet hover:border-line-2 hover:bg-sheet-2',
            )}
        >
            <ThemePreview theme={theme} />
            <span className="flex min-w-0 flex-col px-0.5">
                <span className="truncate text-[0.78125rem] font-medium text-ink">{label}</span>
                <span className="truncate text-[0.6875rem] text-ink-3">{detail}</span>
            </span>
        </button>
    );
}

function NewThemeCard({ onCreate }: { onCreate: () => void }) {
    return (
        <button
            type="button"
            onClick={onCreate}
            title="New theme (starts from the active one)"
            className="flex min-h-[6.625rem] flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line-2 text-[0.78125rem] text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink"
        >
            <Plus className="size-4" />
            New theme
        </button>
    );
}

/** One colour: a picker, the value as text (any CSS colour, alpha included,
 *  unless `accepts` narrows it) and, while it is overridden, a reset to the
 *  base. */
function TokenRow({ spec, value, overridden, derived = false, accepts = isColor, onSet, onReset }: {
    /** The text field's name and the row's label. */
    spec: { key: string; label: string };
    value: string;
    overridden: boolean;
    /** Not overridden, and following its accent rather than the base. */
    derived?: boolean;
    /** Whether typed text is a value this row takes. */
    accepts?: (value: string) => boolean;
    onSet: (value: string, fromPicker: boolean) => void;
    onReset: () => void;
}) {
    const id = useId();
    // The text being typed; null while the field is not focused.
    const [draft, setDraft] = useState<string | null>(null);
    return (
        <div className="flex h-[1.875rem] items-center gap-2">
            <label htmlFor={id} className={cn(formLabel, 'w-[4.75rem] flex-none truncate')}>{spec.label}</label>
            <input
                type="color"
                aria-label={`${spec.label} picker`}
                value={hexOf(value) ?? ''}
                onChange={e => onSet(e.target.value, true)}
                className="h-[1.375rem] w-[1.875rem] flex-none cursor-pointer rounded-xs border border-line-2 bg-transparent p-0.5 [&::-moz-color-swatch]:rounded-[0.125rem] [&::-moz-color-swatch]:border-none [&::-webkit-color-swatch-wrapper]:p-0 [&::-webkit-color-swatch]:rounded-[0.125rem] [&::-webkit-color-swatch]:border-none"
            />
            <input
                id={id}
                type="text"
                name={spec.key}
                spellCheck={false}
                value={draft ?? value}
                onFocus={() => setDraft(value)}
                onBlur={() => setDraft(null)}
                onChange={e => {
                    setDraft(e.target.value);
                    const next = e.target.value.trim();
                    if (next && accepts(next)) onSet(next, false);
                }}
                className={cn(formInput, 'h-[1.625rem] min-w-0 px-2 font-mono text-[0.75rem]')}
            />
            {overridden ? (
                <button
                    type="button"
                    onClick={onReset}
                    title="Back to the base"
                    aria-label={`Reset ${spec.label}`}
                    className={cn(iconBtn, 'size-[1.375rem]')}
                >
                    <RotateCcw className="size-3" />
                </button>
            ) : (
                <span className="w-[1.375rem] flex-none text-center text-[0.65625rem] text-ink-3" title={derived ? 'Follows its accent' : undefined}>
                    {derived ? 'auto' : ''}
                </span>
            )}
        </div>
    );
}

function ThemeEditor({ theme }: { theme: UserTheme }) {
    const {
        renameUserTheme, deleteUserTheme, setUserThemeBase, setUserThemeScheme,
        setUserThemeVar, resetUserThemeVar, setUserThemeFont, setUserThemeScene,
    } = useThemeContext();
    const [confirmingDelete, setConfirmingDelete] = useState(false);
    const nameId = useId();
    const baseId = useId();
    const { vars, scene } = userThemeToDef(theme);
    const baseVars = getTheme(theme.baseThemeId).vars;

    const setToken = (key: ThemeVarKey, value: string, fromPicker: boolean) => {
        // A picked soft fill keeps the base's alpha; typed values are verbatim.
        const next = fromPicker && isSoftAccent(key) ? softVariant(value, baseVars[key]) : value;
        setUserThemeVar(theme.id, key, next);
    };

    return (
        <section className={card} aria-label="Theme editor">
            <div className={cardHeader}>
                <span className={panelLabel}>Edit theme</span>
            </div>
            <div className="space-y-5 p-3">
                <div className="flex flex-col gap-3">
                    <div className="flex flex-col gap-1">
                        <label htmlFor={nameId} className={formLabel}>Name</label>
                        <input
                            id={nameId}
                            value={theme.label}
                            onChange={e => renameUserTheme(theme.id, e.target.value)}
                            className={formInput}
                        />
                    </div>
                    <div className="flex flex-col gap-1">
                        <label htmlFor={baseId} className={formLabel}>Base theme</label>
                        <select
                            id={baseId}
                            value={theme.baseThemeId}
                            onChange={e => { if (isBuiltinThemeId(e.target.value)) setUserThemeBase(theme.id, e.target.value); }}
                            className={formSelect}
                        >
                            {THEMES.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
                        </select>
                    </div>
                    <div className="flex flex-col gap-1">
                        <span className={formLabel}>Scheme</span>
                        <div role="group" aria-label="Scheme" className="flex flex-wrap gap-1">
                            {(['dark', 'light'] as const).map(s => (
                                <button
                                    key={s}
                                    type="button"
                                    aria-pressed={theme.scheme === s}
                                    onClick={() => setUserThemeScheme(theme.id, s)}
                                    className={theme.scheme === s ? segmentOn : segmentOff}
                                >
                                    {s === 'dark' ? 'Dark' : 'Light'}
                                </button>
                            ))}
                        </div>
                    </div>
                </div>

                {TOKEN_GROUPS.map(group => (
                    <fieldset key={group.title} className="space-y-1">
                        <legend className={cn(sectionLabel, 'mb-1.5')}>{group.title}</legend>
                        {group.hint && <p className="pb-1 text-[0.75rem] text-ink-3">{group.hint}</p>}
                        <div className="flex flex-col">
                            {group.tokens.map(spec => {
                                const overridden = theme.vars[spec.key] !== undefined;
                                return (
                                    <TokenRow
                                        key={spec.key}
                                        spec={spec}
                                        value={vars[spec.key]}
                                        overridden={overridden}
                                        derived={!overridden && isSoftAccent(spec.key)
                                            && theme.vars[SOFT_ACCENTS[spec.key]] !== undefined}
                                        onSet={(value, fromPicker) => setToken(spec.key, value, fromPicker)}
                                        onReset={() => resetUserThemeVar(theme.id, spec.key)}
                                    />
                                );
                            })}
                        </div>
                    </fieldset>
                ))}

                <BackgroundFieldset theme={theme} />

                <fieldset className="space-y-1">
                    <legend className={cn(sectionLabel, 'mb-1.5')}>Radius</legend>
                    <div role="group" aria-label="Radius" className="flex flex-wrap gap-1">
                        {RADIUS_OPTIONS.map(r => (
                            <button
                                key={r}
                                type="button"
                                aria-pressed={vars.radius === r}
                                onClick={() => setUserThemeVar(theme.id, 'radius', r)}
                                className={vars.radius === r ? segmentOn : segmentOff}
                            >
                                {r}
                            </button>
                        ))}
                    </div>
                </fieldset>

                <fieldset className="space-y-1">
                    <legend className={cn(sectionLabel, 'mb-1.5')}>Selection edge</legend>
                    <div role="group" aria-label="Selection edge" className="flex flex-wrap gap-1">
                        {SELECTION_EDGES.map(edge => (
                            <button
                                key={edge}
                                type="button"
                                aria-pressed={scene.edge === edge}
                                onClick={() => setUserThemeScene(theme.id, 'edge', edge)}
                                className={scene.edge === edge ? segmentOn : segmentOff}
                            >
                                {edge}px
                            </button>
                        ))}
                    </div>
                </fieldset>

                <fieldset className="space-y-1">
                    <legend className={cn(sectionLabel, 'mb-1.5')}>Fonts</legend>
                    <div className="flex flex-col gap-3">
                        {FONT_ROWS.map(row => (
                            <FontRow
                                key={row.role}
                                label={row.label}
                                role={row.role}
                                sample={row.sample}
                                stack={vars[FONT_ROLE_VARS[row.role]]}
                                onChange={family => setUserThemeFont(theme.id, row.role, family)}
                            />
                        ))}
                    </div>
                </fieldset>
            </div>
            {confirmingDelete ? (
                <div className={confirmStrip}>
                    <span className="mr-auto">Delete “{theme.label || 'Untitled'}”?</span>
                    <button type="button" className={btnDanger} onClick={() => deleteUserTheme(theme.id)}>Delete</button>
                    <button type="button" className={btn} onClick={() => setConfirmingDelete(false)}>Keep</button>
                </div>
            ) : (
                <div className="flex justify-end border-t border-line px-3 py-2">
                    <button type="button" className={btnDanger} onClick={() => setConfirmingDelete(true)}>Delete theme</button>
                </div>
            )}
        </section>
    );
}

/** The reset to the base while a field is overridden, else its empty slot. */
function ResetSlot({ label, overridden, onReset }: { label: string; overridden: boolean; onReset: () => void }) {
    return overridden ? (
        <button
            type="button"
            onClick={onReset}
            title="Back to the base"
            aria-label={`Reset ${label}`}
            className={cn(iconBtn, 'size-[1.375rem]')}
        >
            <RotateCcw className="size-3" />
        </button>
    ) : <span className="w-[1.375rem] flex-none" />;
}

/** A 0..1 slider with its value as a percentage, and a reset while it is
 *  overridden: a row like a token's, its label beside it. */
function UnitRow({ name, label, value, overridden, onSet, onReset }: {
    name: string; label: string; value: number; overridden: boolean;
    onSet: (value: number) => void; onReset: () => void;
}) {
    const id = useId();
    return (
        <div className="flex h-[1.875rem] items-center gap-2">
            <label htmlFor={id} className={cn(formLabel, 'w-[4.75rem] flex-none truncate')}>{label}</label>
            <input
                id={id}
                name={name}
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={value}
                onChange={e => onSet(Number(e.target.value))}
                className="h-1.5 min-w-0 flex-1 cursor-pointer accent-teal"
            />
            <span className="w-[2.25rem] flex-none text-right font-mono text-[0.6875rem] text-ink-3">{Math.round(value * 100)}%</span>
            <ResetSlot label={label} overridden={overridden} onReset={onReset} />
        </div>
    );
}

/** A scene option that is on or off: its label, the settings switch, and a
 *  reset while it is overridden. */
function SwitchRow({ label, checked, overridden, onSet, onReset }: {
    label: string; checked: boolean; overridden: boolean;
    onSet: (value: boolean) => void; onReset: () => void;
}) {
    return (
        <div className="flex h-[1.875rem] items-center gap-2">
            <span className={cn(formLabel, 'min-w-0 flex-1 truncate')}>{label}</span>
            <Switch checked={checked} label={label} onChange={onSet} />
            <ResetSlot label={label} overridden={overridden} onReset={onReset} />
        </div>
    );
}

/** The scene's picture: a thumbnail of the one set, a file choice that
 *  uploads an image into the CAS and sets its hash, a Remove while one is
 *  set, and a reset while the field is overridden. */
function ImageRow({ image, overridden, onSet, onReset }: {
    image: string | null; overridden: boolean;
    onSet: (image: string | null) => void; onReset: () => void;
}) {
    const label = 'Image';
    const channel = useThemeChannel();
    const input = useRef<HTMLInputElement>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const pick = async (file: File | undefined) => {
        if (input.current) input.current.value = '';
        if (!file) return;
        if (!file.type.startsWith('image/')) {
            setError('Not an image');
            return;
        }
        setBusy(true);
        setError(null);
        try {
            onSet((await streamBlob(channel, file)).blob_hash);
        } catch (e) {
            setError(e instanceof Error && e.message ? e.message : 'Upload failed');
        } finally {
            setBusy(false);
        }
    };
    return (
        <div className="flex h-[1.875rem] items-center gap-2">
            <span className={cn(formLabel, 'w-[4.75rem] flex-none truncate')}>{label}</span>
            {image ? (
                <img
                    src={blobUrl(channel, image)}
                    alt=""
                    data-scene-image={image}
                    className="h-[1.375rem] w-[1.875rem] flex-none rounded-xs border border-line-2 object-cover"
                />
            ) : (
                <span aria-hidden className="h-[1.375rem] w-[1.875rem] flex-none rounded-xs border border-dashed border-line-2" />
            )}
            <input
                ref={input}
                type="file"
                accept="image/*"
                aria-label="Choose image"
                className="sr-only"
                onChange={e => { void pick(e.target.files?.[0]); }}
            />
            <button type="button" disabled={busy} onClick={() => input.current?.click()} className={segmentOff}>
                {busy ? 'Uploading…' : image ? 'Change…' : 'Choose…'}
            </button>
            {image && (
                <button type="button" onClick={() => onSet(null)} className={segmentOff}>Remove</button>
            )}
            <span role={error ? 'alert' : undefined} className="min-w-0 flex-1 truncate text-[0.6875rem] text-red">{error}</span>
            <ResetSlot label={label} overridden={overridden} onReset={onReset} />
        </div>
    );
}

/** The scene's two reach options: where it runs on behind the chrome. */
const REACHES = [
    { key: 'behindTopBar', label: 'Behind the top bar' },
    { key: 'behindRemoved', label: 'Behind the removed shelf' },
] as const;

/** A glow: on or off, its colour and its strength. */
function GlowRow({ label, glow, start, overridden, onSet, onReset }: {
    label: string;
    glow: SceneGlow | null;
    /** What switching it on starts from. */
    start: SceneGlow;
    overridden: boolean;
    onSet: (glow: SceneGlow | null) => void;
    onReset: () => void;
}) {
    const id = useId();
    const value = glow ?? start;
    return (
        <div className="flex h-[1.875rem] items-center gap-2">
            <label htmlFor={id} className={cn(formLabel, 'flex w-[4.75rem] flex-none items-center gap-1.5 truncate')}>
                <input
                    id={id}
                    type="checkbox"
                    checked={glow !== null}
                    onChange={e => onSet(e.target.checked ? value : null)}
                    className="flex-none accent-teal"
                />
                <span className="truncate">{label}</span>
            </label>
            <input
                type="color"
                aria-label={`${label} colour`}
                disabled={glow === null}
                value={value.color}
                onChange={e => onSet({ ...value, color: e.target.value })}
                className="h-[1.375rem] w-[1.875rem] flex-none cursor-pointer rounded-xs border border-line-2 bg-transparent p-0.5 disabled:cursor-default disabled:opacity-40 [&::-moz-color-swatch]:rounded-[0.125rem] [&::-moz-color-swatch]:border-none [&::-webkit-color-swatch-wrapper]:p-0 [&::-webkit-color-swatch]:rounded-[0.125rem] [&::-webkit-color-swatch]:border-none"
            />
            <input
                type="range"
                aria-label={`${label} strength`}
                disabled={glow === null}
                min={0}
                max={1}
                step={0.01}
                value={value.alpha}
                onChange={e => onSet({ ...value, alpha: Number(e.target.value) })}
                className="h-1.5 min-w-0 flex-1 cursor-pointer accent-teal disabled:cursor-default disabled:opacity-40"
            />
            <ResetSlot label={label} overridden={overridden} onReset={onReset} />
        </div>
    );
}

/** The scene behind the stacks: the pattern with its pitch, whether it runs
 *  on behind the top bar and between the removed cards, its strength, the
 *  ground and the pattern's ink, the two glows and the vignette. Each field
 *  overrides the base's scene and resets on its own. */
function BackgroundFieldset({ theme }: { theme: UserTheme }) {
    const { setUserThemeScene, resetUserThemeScene } = useThemeContext();
    const { vars, scene } = userThemeToDef(theme);
    const base = getTheme(theme.baseThemeId).scene;
    const overridden = (key: keyof ThemeScene) => theme.scene[key] !== undefined;
    const reset = (key: keyof ThemeScene) => () => resetUserThemeScene(theme.id, key);
    return (
        <fieldset className="space-y-1">
            <legend className={cn(sectionLabel, 'mb-1.5')}>Background</legend>
            <p className="pb-1 text-[0.75rem] text-ink-3">The ground behind the stacks: a colour or a picture of your own, under the pattern. The bars and back cards keep the canvas; the two switches let it run on behind the top bar and between the removed cards.</p>
            <div role="group" aria-label="Pattern" className="flex flex-wrap gap-1">
                {SCENE_PATTERNS.map(pattern => (
                    <button
                        key={pattern}
                        type="button"
                        aria-pressed={scene.pattern === pattern}
                        onClick={() => setUserThemeScene(theme.id, 'pattern', pattern)}
                        className={scene.pattern === pattern ? segmentOn : segmentOff}
                    >
                        {PATTERN_LABELS[pattern]}
                    </button>
                ))}
            </div>
            <div role="group" aria-label="Pitch" title="Minor / major pitch" className="flex flex-wrap gap-1 pb-1">
                {SCENE_PITCHES.map(pitch => (
                    <button
                        key={pitch}
                        type="button"
                        aria-pressed={scene.pitch === pitch}
                        onClick={() => setUserThemeScene(theme.id, 'pitch', pitch)}
                        className={scene.pitch === pitch ? segmentOn : segmentOff}
                    >
                        {pitch} / {pitch * SCENE_MAJOR}
                    </button>
                ))}
            </div>
            <div className="flex flex-col">
                {REACHES.map(({ key, label }) => (
                    <SwitchRow
                        key={key}
                        label={label}
                        checked={scene[key]}
                        overridden={overridden(key)}
                        onSet={value => setUserThemeScene(theme.id, key, value)}
                        onReset={reset(key)}
                    />
                ))}
                <UnitRow
                    name="scene.strength"
                    label="Strength"
                    value={scene.strength}
                    overridden={overridden('strength')}
                    onSet={v => setUserThemeScene(theme.id, 'strength', v)}
                    onReset={reset('strength')}
                />
                <TokenRow
                    spec={{ key: 'scene.base', label: 'Ground' }}
                    value={sceneGround(scene, vars)}
                    overridden={overridden('base')}
                    accepts={isSceneColor}
                    onSet={value => setUserThemeScene(theme.id, 'base', value)}
                    onReset={reset('base')}
                />
                <ImageRow
                    image={scene.image}
                    overridden={overridden('image')}
                    onSet={image => setUserThemeScene(theme.id, 'image', image)}
                    onReset={reset('image')}
                />
                <TokenRow
                    spec={{ key: 'scene.ink', label: 'Pattern ink' }}
                    value={scene.ink}
                    overridden={overridden('ink')}
                    accepts={isSceneColor}
                    onSet={value => setUserThemeScene(theme.id, 'ink', value)}
                    onReset={reset('ink')}
                />
                {GLOWS.map(({ key, label }) => (
                    <GlowRow
                        key={key}
                        label={label}
                        glow={scene[key]}
                        start={base[key] ?? { color: scene.ink, alpha: GLOW_START_ALPHA }}
                        overridden={overridden(key)}
                        onSet={glow => setUserThemeScene(theme.id, key, glow)}
                        onReset={reset(key)}
                    />
                ))}
                <UnitRow
                    name="scene.vignette"
                    label="Vignette"
                    value={scene.vignette}
                    overridden={overridden('vignette')}
                    onSet={v => setUserThemeScene(theme.id, 'vignette', v)}
                    onReset={reset('vignette')}
                />
            </div>
        </fieldset>
    );
}

/** One font role: its label over a select of the bundled catalogue, and a
 *  sample in the chosen face under it. A built-in may set a face from another
 *  role (a mono interface): it is listed first, so the select shows it. */
function FontRow({ label, role, sample, stack, onChange }: {
    label: string; role: FontRole; sample: string; stack: string; onChange: (family: string) => void;
}) {
    const id = useId();
    const current = familyForStack(stack);
    return (
        <div className="flex flex-col gap-1">
            <label htmlFor={id} className={formLabel}>{label}</label>
            <select
                id={id}
                value={current?.family ?? ''}
                onChange={e => onChange(e.target.value)}
                className={formSelect}
            >
                {!current && <option value="" disabled>Choose a face</option>}
                {current && current.role !== role && <option value={current.family}>{current.label}</option>}
                {FONT_CATALOGUE[role].map(f => <option key={f.family} value={f.family}>{f.label}</option>)}
            </select>
            <span className="truncate text-[0.84375rem] text-ink-2" style={{ fontFamily: stack }}>{sample}</span>
        </div>
    );
}

export function ThemeSection() {
    const { themeId, setThemeId, userThemes, createUserTheme } = useThemeContext();
    const editing = userThemes.find(u => u.id === themeId) ?? null;

    return (
        <div className="space-y-5">
            <section className="space-y-2" aria-label="Themes">
                <h4 className={sectionLabel}>Themes</h4>
                <div className="grid grid-cols-2 gap-2">
                    {THEMES.map(theme => (
                        <ThemeCard
                            key={theme.id}
                            theme={theme}
                            detail={`Built-in · ${theme.scheme}`}
                            selected={themeId === theme.id}
                            onSelect={() => setThemeId(theme.id)}
                        />
                    ))}
                    {userThemes.map(u => (
                        <ThemeCard
                            key={u.id}
                            theme={userThemeToDef(u)}
                            detail={`${getTheme(u.baseThemeId).label} · ${u.scheme}`}
                            selected={themeId === u.id}
                            onSelect={() => setThemeId(u.id)}
                        />
                    ))}
                    <NewThemeCard onCreate={createUserTheme} />
                </div>
                {!editing && (
                    <p className="text-[0.75rem] text-ink-3">
                        Built-in themes are read-only. New theme makes an editable copy of the active one.
                    </p>
                )}
            </section>
            {editing && <ThemeEditor key={editing.id} theme={editing} />}
        </div>
    );
}
