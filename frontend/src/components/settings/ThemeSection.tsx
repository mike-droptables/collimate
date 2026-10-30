import { useEffect, useId, useState } from 'react';
import { Plus, RotateCcw } from 'lucide-react';
import { useThemeContext } from '@/contexts/ThemeContext';
import { ThemeDef, ThemeVarKey, ThemeVars, THEMES, getTheme, isBuiltinThemeId, softVariant } from '@/lib/themes';
import { FONT_CATALOGUE, FontRole, familyForStack } from '@/lib/fonts';
import {
    UserTheme, FONT_ROLE_VARS, SOFT_ACCENTS, isSoftAccent, hexOf, userThemeToDef,
} from '@/lib/userThemes';
import {
    btn, btnDanger, card, cardHeader, confirmStrip, formInput, formLabel, formSelect, iconBtn,
    panelLabel, sectionLabel, segmentOff, segmentOn,
} from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { desktopBridge } from '@/lib/desktop';
import {
    INTERFACE_SCALES, InterfaceScale, canSetInterfaceScale,
    getInterfaceScale, interfaceScaleLabel, setInterfaceScale,
} from '@/lib/interfaceScale';

// ─── Theme section ───────────────────────────────────────────────────────────
// One column in the Commands pane's settings view. In the desktop shell,
// first the interface size: this device's relative page zoom,
// not the channel's. Then the theme cards two to a row: the three built-ins,
// the channel's user themes, then "+", which makes a user theme from the
// active one. Clicking a card applies it. Built-ins are read-only; the editor
// opens under the cards for the active user theme: its name, base and
// scheme stacked, the token pickers by group, the radius and the three font
// roles. Shadow and lift always come from the base.

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

const RADIUS_OPTIONS = ['4px', '6px', '8px', '10px', '12px'];

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
 *  canvas, two ink lines, a selected row in brass and a live dot in teal. */
function ThemePreview({ vars }: { vars: ThemeVars }) {
    return (
        <div
            aria-hidden
            className="h-14 overflow-hidden rounded-md p-1.5"
            style={{ background: vars.canvas, border: `1px solid ${vars.line}` }}
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
            <ThemePreview vars={theme.vars} />
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

/** One token: a colour picker, the value as text (any CSS colour, alpha
 *  included) and, while it is overridden, a reset to the base. */
function TokenRow({ spec, value, overridden, derived, onSet, onReset }: {
    spec: TokenSpec;
    value: string;
    overridden: boolean;
    /** Not overridden, and following its accent rather than the base. */
    derived: boolean;
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
                    if (next && isColor(next)) onSet(next, false);
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
        setUserThemeVar, resetUserThemeVar, setUserThemeFont,
    } = useThemeContext();
    const [confirmingDelete, setConfirmingDelete] = useState(false);
    const nameId = useId();
    const baseId = useId();
    const vars = userThemeToDef(theme).vars;
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

/** One font role: its label over a select of the bundled catalogue, and a
 *  sample in the chosen face under it. */
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
                value={current?.role === role ? current.family : ''}
                onChange={e => onChange(e.target.value)}
                className={formSelect}
            >
                {current?.role !== role && <option value="" disabled>Choose a face</option>}
                {FONT_CATALOGUE[role].map(f => <option key={f.family} value={f.family}>{f.label}</option>)}
            </select>
            <span className="truncate text-[0.84375rem] text-ink-2" style={{ fontFamily: stack }}>{sample}</span>
        </div>
    );
}

/** The scale the page zoom `factor` stands for, if it is one. */
const scaleAt = (factor: number): InterfaceScale | undefined =>
    INTERFACE_SCALES.find(scale => Math.abs(scale - factor) < 0.005);

/** This device's interface size, as a segmented row. It follows a change
 *  made elsewhere (the View menu) through the shell's zoom signal. */
function InterfaceSizeRow() {
    const labelId = useId();
    const [scale, setScale] = useState<InterfaceScale | null>(() => getInterfaceScale());
    useEffect(() => desktopBridge()?.onZoomChanged((factor) => {
        setScale(scaleAt(factor) ?? getInterfaceScale());
    }), []);
    const choose = (next: InterfaceScale) => {
        if (setInterfaceScale(next)) setScale(next);
    };
    return (
        <section className="space-y-2" data-interface-size="">
            <div className="flex items-baseline gap-2">
                <h4 id={labelId} className={sectionLabel}>Interface size</h4>
                <span className="text-[0.75rem] text-ink-3">This device</span>
            </div>
            <div role="group" aria-labelledby={labelId} className="flex flex-wrap gap-1">
                {INTERFACE_SCALES.map(option => (
                    <button
                        key={option}
                        type="button"
                        aria-pressed={scale === option}
                        onClick={() => choose(option)}
                        className={scale === option ? segmentOn : segmentOff}
                    >
                        {interfaceScaleLabel(option)}
                    </button>
                ))}
            </div>
        </section>
    );
}

export function ThemeSection() {
    const { themeId, setThemeId, userThemes, createUserTheme } = useThemeContext();
    const editing = userThemes.find(u => u.id === themeId) ?? null;
    // Only the desktop shell can scale the whole window; a browser's own
    // zoom does it there.
    const [showInterfaceSize] = useState(canSetInterfaceScale);

    return (
        <div className="space-y-5">
            {showInterfaceSize && <InterfaceSizeRow />}
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
