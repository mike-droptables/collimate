import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ComponentType, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import { ChevronLeft, Search } from 'lucide-react';
import type { PickKind } from '@/lib/types';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { RendererConcealContext, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { COMMAND_PANE_ATTR, PANE_ENTRY_ATTR, focusCellCard } from '@/lib/commandPane';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { btn, btnDanger, chip, confirmStrip, kbd, modeTab, modeTabOff, modeTabOn, panelLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** A status tag: a dot in its tone before the label. `pass` is green (an
 *  accepted handler, as on the Manage view), `live` is teal, no tone is
 *  ink-3. */
export interface CellPanelStatusTag {
    label: string;
    tone?: 'pass' | 'live';
}

/** One row of a panel's list. */
export interface CellPanelRow {
    id: string;
    name: string;
    description?: string;
    /** A plain string is a quiet chip; a status tag carries a tone. */
    tags?: readonly (string | CellPanelStatusTag)[];
    /** Inert: shown greyed; a (double-)click does not run the primary. */
    disabled?: boolean;
    /** Why the row cannot be used: red on a live row (a broken runner),
     *  ink-3 on a disabled one (a renderer that does not claim the file). */
    reason?: string;
    /** A quiet line after the description (a runner's "will use <file>"). */
    note?: string;
}

/** A quiet action (the mock's `qbtn`) beside the search field: its label
 *  hides in a narrow panel. */
export interface CellPanelAction {
    id: string;
    label: string;
    icon?: ComponentType<{ className?: string }>;
    disabled?: boolean;
    /** A toggle (a filter): announced through aria-pressed, teal while on. */
    pressed?: boolean;
    onRun: () => void;
}

/** A mode tab. A disabled one is greyed and skipped by Tab; `reason` is
 *  its title (why it cannot be chosen). */
export interface CellPanelMode<M extends string = string> {
    id: M;
    label: string;
    disabled?: boolean;
    reason?: string;
}

/** The footer's primary button (the mock's `rp-go`). Its icon is brass for
 *  `run`, quiet for `edit`. ↵ runs it. */
export interface CellPanelPrimary {
    label: string;
    icon: ComponentType<{ className?: string }>;
    intent: 'run' | 'edit';
    disabled?: boolean;
    /** The disabled button's title: why it cannot run. */
    reason?: string;
    onRun: () => void;
}

/** A second footer action beside the primary, run by Shift+Enter
 *  (the runners panel's "in new cell ⇧↵"). In a narrow panel only its key
 *  hint shows. */
export interface CellPanelSecondary {
    label: string;
    /** The key hint shown in the button, e.g. "⇧↵". */
    hint: string;
    disabled?: boolean;
    onRun: () => void;
}

/** The empty list's way out: a button under its text. */
export interface CellPanelEmptyAction {
    label: string;
    onSelect: () => void;
}

/** The inline "Delete ‘x’? [Delete] [Keep]" strip above the footer, or
 *  under a step's body. */
export interface CellPanelConfirm {
    message: string;
    confirmLabel: string;
    busy?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}

/** A step body (a form or a summary) that replaces the modes, the search,
 *  the list and the footer; the header shows Back. Esc goes back. */
export interface CellPanelStep {
    body: ReactNode;
    onBack: () => void;
}

export type CellPanelCloseReason = 'cancel' | 'close' | 'scrim';

/** Where a panel renders: over its cell's body, or in the Commands pane's
 *  panel slot (a picker of the selected cell while the pane is open;
 *  paneHostsPanel). */
export type CellPanelHost = 'cell' | 'pane';

export interface CellPanelProps<M extends string = string> {
    cellId: string;
    kind: PickKind;
    /** In its cell unless the cell editor hands it to the pane. */
    host?: CellPanelHost;
    title: string;
    /** Toggles beside the search field (the Files panel's Runs and Recent):
     *  they filter the list, so they sit with the field. */
    searchActions?: readonly CellPanelAction[];
    note?: ReactNode;
    modes?: readonly CellPanelMode<M>[];
    mode?: M;
    onMode?: (mode: M) => void;
    /** With `onQuery`, the search field: the panel's only text input. */
    query?: string;
    onQuery?: (query: string) => void;
    placeholder?: string;
    /** The field's leading icon: a magnifier unless the field means
     *  something else (the Files panel's Create mode takes a name). */
    searchIcon?: ComponentType<{ className?: string }>;
    /** A failure to report under the search field. */
    error?: string;
    /** A quiet line between the mode tabs and the field: where the field
     *  acts (the Files panel's Browse folder). */
    location?: ReactNode;
    /** A quiet line under the field (the default renderer of the name being
     *  typed). */
    fieldNote?: ReactNode;
    /** A ghost suffix after the field's text: the inline completion the
     *  caller's Tab would accept. */
    completion?: string;
    /** Sees each key in the field first. True makes the key the caller's:
     *  the panel stops it and does nothing more, so a field that means a
     *  name can keep ↑↓, Tab, ↵ and Esc from the list, the modes and the
     *  close. */
    onFieldKey?: (e: ReactKeyboardEvent<HTMLInputElement>) => boolean;
    /** Esc once no confirm strip or step wants it: true keeps the panel
     *  open (the Files panel's Browse goes up a folder). */
    onEscape?: () => boolean;
    rows?: readonly CellPanelRow[];
    selectedId?: string | null;
    onSelect?: (id: string) => void;
    /** A click runs its row's primary, as ↵ does (the Files panel, like the
     *  Commands pane's rows), instead of only selecting the row. */
    clickRuns?: boolean;
    /** The list's text when it has no rows. */
    empty?: ReactNode;
    /** A button under the empty text that leads where rows are made (the
     *  runners panel's 'Open Capabilities'). */
    emptyAction?: CellPanelEmptyAction;
    primary?: CellPanelPrimary;
    secondary?: CellPanelSecondary;
    /** Footer hints after "↑↓ select". */
    hints?: readonly { keys: string; label: string }[];
    confirm?: CellPanelConfirm;
    step?: CellPanelStep;
    onClose: (reason: CellPanelCloseReason) => void;
}

/** The pooled renderer iframes live in a world-space overlay ABOVE the
 *  cell's DOM, so a panel over the body conceals them while it is open (the
 *  cell editor wraps its content pane in this). Native guests are frozen by
 *  the panel itself. */
export function CellPanelConceal({ open, children }: { open: boolean; children: ReactNode }) {
    const parent = useRendererConcealed();
    return <RendererConcealContext.Provider value={parent || open}>{children}</RendererConcealContext.Provider>;
}

/** Hand the keyboard back after a panel closes: to the Commands pane's
 *  entry field while the pane is open (its query, or its settings view's
 *  entry; a panel the pane hosted leaves the pane's commands to come back,
 *  and the pane puts the keyboard on their query as they do), else to the
 *  cell's card. */
function returnFocus(cellId: string) {
    if (useCollimateStore.getState().commandPane.open) {
        document.querySelector<HTMLElement>(`[${COMMAND_PANE_ATTR}] [${PANE_ENTRY_ATTR}]`)?.focus({ preventScroll: true });
        return;
    }
    focusCellCard(cellId);
}

const FOOT_HINT = 'inline-flex items-center gap-[0.3125rem]';
const TAG_TONE = { pass: 'bg-green', live: 'bg-teal' } as const;

function RowTag({ tag }: { tag: string | CellPanelStatusTag }) {
    if (typeof tag === 'string') return <span className={chip}>{tag}</span>;
    return <span className={chip} data-tone={tag.tone ?? 'none'}>
        <span aria-hidden className={cn('size-1.5 flex-none rounded-full', tag.tone ? TAG_TONE[tag.tone] : 'bg-ink-3')} />
        {tag.label}
    </span>;
}
/** A toggle while it is on: teal, the live/on-state colour. */
const ACTION_ON = 'border-teal bg-teal-soft text-teal hover:bg-teal-soft hover:text-teal';

/** A search action: a toggle leaves the keyboard in the search field. */
function ActionButton({ action }: { action: CellPanelAction }) {
    const Icon = action.icon;
    return <button type="button" className={cn(btn, 'flex-none', action.pressed && ACTION_ON)} aria-label={action.label} title={action.label}
        aria-pressed={action.pressed} disabled={action.disabled} onMouseDown={e => e.preventDefault()} onClick={action.onRun}>
        {Icon && <Icon className="h-3.5 w-3.5" />}
        <span className="@max-[18.75rem]:hidden">{action.label}</span>
    </button>;
}

const PRIMARY = 'inline-flex h-[1.625rem] min-w-0 flex-[0_1_auto] items-center gap-1.5 rounded-sm border border-line-2 bg-sheet px-2.5 text-[0.78125rem] font-semibold text-ink shadow-[inset_0_0_0_0.5px_var(--line)] transition-colors hover:bg-sheet-3 disabled:opacity-45 disabled:cursor-default';
const PRIMARY_INTENT: Record<CellPanelPrimary['intent'], string> = {
    run: '[&_svg]:text-brass',
    edit: '[&_svg]:text-ink-3',
};

/** The sheet over the cell's body: at most 640px wide, centred with at
 *  least 12px either side and 12px above and below (no transform, so its
 *  container query keeps measuring the sheet). */
const SHEET_IN_CELL = 'absolute inset-x-[max(0.75rem,calc((100%-40rem)/2))] top-6 bottom-6 z-5 rounded-lg border border-line-2 shadow-(--shadow)';
/** The pane's slot: the panel fills the column under the target row. */
const SHEET_IN_PANE = 'relative flex-1 min-h-0';

/**
 * The cell panel (row P01, the mock's runners panel): a sheet with a
 * header (title, Close), an optional note, mode tabs, an optional location
 * line, a field (a search, or whatever the mode makes of it, with an
 * optional ghost completion and a note under it), a listbox, an optional
 * confirm strip and a footer with hints and the primary button. A step
 * replaces everything under the header with its own body and Back; the
 * confirm strip can ask its question under a step's body too.
 *
 * The cell editor mounts one per cell, by the kind of the cell's pick
 * session. In the cell (`host` 'cell') it lies inside the pane region
 * (from under the cell head to the cell's bottom): the skin's
 * scrim (`--lift`) over the cell's body and the inset sheet, a keyboard
 * owner, freezing the cell's native guests. In the Commands pane (`host`
 * 'pane', a picker of the selected cell while the pane is open) it fills
 * the pane's panel slot flat, with no scrim: the cell shows its normal
 * state, the scene's chords keep working from its search as from the
 * pane's query, and the pane's own occluder covers what lies under it.
 *
 * Keyboard: ↑↓ move the selection (wrapping), Tab / Shift+Tab cycle the
 * modes, ↵ runs the primary, Shift+↵ the secondary, Esc closes (or
 * cancels the confirm strip, or goes Back from a step, or does what the
 * kind's onEscape makes of it). The search box keeps every one of those
 * keys, unless the kind's onFieldKey takes one first. A click selects a
 * row and a double-click runs it, or with `clickRuns` a click runs it.
 */
export function CellPanel<M extends string = string>(props: CellPanelProps<M>) {
    const {
        cellId, kind, host = 'cell', title, searchActions, note, modes, mode, onMode, query, onQuery, placeholder, searchIcon: SearchIcon = Search, error,
        location, fieldNote, completion, onFieldKey, onEscape, rows, selectedId, onSelect, clickRuns, empty, emptyAction, primary, secondary, hints, confirm, step, onClose,
    } = props;
    const panelRef = useRef<HTMLDivElement>(null);
    const searchRef = useRef<HTMLInputElement>(null);
    const listRef = useRef<HTMLDivElement>(null);
    const modeGroupRef = useRef<HTMLDivElement>(null);
    // A mode row wider than the panel (the Commands pane's column, a narrow
    // cell) scrolls sideways: the active mode, by click or Tab, is kept in
    // view.
    useLayoutEffect(() => {
        const group = modeGroupRef.current;
        const on = group?.querySelector<HTMLElement>('[aria-pressed="true"]');
        if (!group || !on) return;
        if (on.offsetLeft < group.scrollLeft) group.scrollLeft = on.offsetLeft;
        else if (on.offsetLeft + on.offsetWidth > group.scrollLeft + group.clientWidth) group.scrollLeft = on.offsetLeft + on.offsetWidth - group.clientWidth;
    }, [mode]);
    const stepRef = useRef<HTMLDivElement>(null);
    // The ghost completion follows the field's text when a long value
    // scrolls it sideways.
    const [fieldScroll, setFieldScroll] = useState(0);
    const baseId = useId();
    const listId = `${baseId}-list`;
    const fieldNoteId = `${baseId}-field-note`;
    const optionId = (id: string) => `${baseId}-option-${id}`;
    const selectedCell = useCellSelected();
    const inCell = host === 'cell';
    useGuestOccluder(useMemo(() => ({ cellIds: [cellId] }), [cellId]), inCell);

    // The keyboard starts in the search box, comes back to it when a step
    // ends, and goes to the step's body while one is up (so Esc reaches the
    // panel and means Back).
    const inStep = !!step;
    useEffect(() => {
        if (inStep) stepRef.current?.focus({ preventScroll: true });
        else (searchRef.current ?? panelRef.current)?.focus({ preventScroll: true });
    }, [inStep]);

    useEffect(() => {
        if (!selectedId) return;
        document.getElementById(`${baseId}-option-${selectedId}`)?.scrollIntoView?.({ block: 'nearest' });
    }, [baseId, selectedId]);

    const close = (reason: CellPanelCloseReason) => {
        onClose(reason);
        returnFocus(cellId);
    };

    // A close the panel did not make (a run started, a hand-over to another
    // panel, a step that ends the session) hands the keyboard back the same
    // way, when the keyboard was in the panel or had nowhere to be (a
    // focused button went disabled). This runs before the panel's nodes
    // leave the document; a panel mounting in its place takes the keyboard
    // after it.
    useLayoutEffect(() => {
        const node = panelRef.current;
        return () => {
            const active = document.activeElement;
            if (!active || active === document.body || node?.contains(active)) returnFocus(cellId);
        };
    }, [cellId]);

    const inPanel = (e: Event) => e.target instanceof Node && !!panelRef.current?.contains(e.target);
    /** List keys act only from the search box, the list or the panel
     *  itself, never from a button or a step's field. */
    const fromList = (e: Event | ReactKeyboardEvent) => {
        const target = e.target as Node | null;
        return !step && !!target && (target === searchRef.current || target === panelRef.current || !!listRef.current?.contains(target));
    };

    const move = (delta: 1 | -1) => {
        if (!rows?.length || !onSelect) return;
        const at = rows.findIndex(row => row.id === selectedId);
        const next = at < 0 ? (delta > 0 ? 0 : rows.length - 1) : (at + delta + rows.length) % rows.length;
        onSelect(rows[next].id);
    };
    const runPrimary = () => { if (primary && !primary.disabled) primary.onRun(); };
    /** Keep (or Esc) puts the keyboard back where the question found it:
     *  the field, else the step's body, else the panel. */
    const cancelConfirm = (strip: CellPanelConfirm) => {
        strip.onCancel();
        (searchRef.current ?? stepRef.current ?? panelRef.current)?.focus({ preventScroll: true });
    };
    // The primary as of the last commit: a run from the list (a double-click,
    // or a click with clickRuns) selects its row and commits that selection
    // before it runs, so the primary it runs (and whether it is enabled) is
    // the clicked row's, not the one selected before.
    const latestPrimary = useRef(primary);
    useLayoutEffect(() => { latestPrimary.current = primary; });
    const runRow = (row: CellPanelRow) => {
        if (onSelect && row.id !== selectedId) flushSync(() => onSelect(row.id));
        const now = latestPrimary.current;
        if (!row.disabled && now && !now.disabled) now.onRun();
    };

    useShortcut(SHORTCUT_IDS.LIST_NEXT, e => { if (!inPanel(e) || !fromList(e)) return; e.preventDefault(); move(1); }, { skipWhenTyping: false });
    useShortcut(SHORTCUT_IDS.LIST_PREV, e => { if (!inPanel(e) || !fromList(e)) return; e.preventDefault(); move(-1); }, { skipWhenTyping: false });
    useShortcut(SHORTCUT_IDS.LIST_RUN_HIGHLIGHTED, e => { if (!inPanel(e) || !fromList(e)) return; e.preventDefault(); runPrimary(); }, { skipWhenTyping: false });
    useShortcut(SHORTCUT_IDS.LIST_CLOSE, e => {
        if (!inPanel(e)) return;
        e.preventDefault();
        // The panel's Esc goes no further: the cell's file editor (its
        // window-level Esc closes the file's tab) never sees it.
        e.stopPropagation();
        if (confirm) { cancelConfirm(confirm); return; }
        if (step) { step.onBack(); return; }
        if (onEscape?.()) return;
        close('cancel');
    }, { skipWhenTyping: false });

    /** Tab / Shift+Tab cycle the modes (skipping disabled ones) and Shift+↵
     *  runs the secondary; neither is a registered shortcut. */
    const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
        if (!fromList(e) || e.altKey || e.ctrlKey || e.metaKey) return;
        if (e.key === 'Tab' && modes && onMode) {
            e.preventDefault();
            const live = modes.filter(m => !m.disabled);
            if (!live.length) return;
            const at = live.findIndex(m => m.id === mode);
            const next = at < 0 ? 0 : (at + (e.shiftKey ? live.length - 1 : 1)) % live.length;
            onMode(live[next].id);
            return;
        }
        if (e.key === 'Enter' && e.shiftKey && secondary) {
            e.preventDefault();
            if (!secondary.disabled) secondary.onRun();
        }
    };

    /** A press anywhere but on a control keeps the keyboard where it is. */
    const keepFocus = (e: ReactMouseEvent) => { if (!(e.target as HTMLElement).closest('button, input')) e.preventDefault(); };
    const selected = rows?.find(row => row.id === selectedId);
    const confirmBar = confirm && <div className={cn(confirmStrip, 'flex-none')}>
        <span className="min-w-0 flex-1">{confirm.message}</span>
        <button type="button" autoFocus className={btnDanger} disabled={confirm.busy} onClick={confirm.onConfirm}>{confirm.confirmLabel}</button>
        <button type="button" className={btn} onClick={() => cancelConfirm(confirm)}>Keep</button>
    </div>;

    return <>
        {inCell && <div
            aria-hidden
            data-cell-panel-scrim=""
            className="absolute inset-0 z-4 bg-(--lift)"
            onClick={() => close('scrim')}
        />}
        <div
            ref={panelRef}
            role="dialog"
            aria-label={title}
            tabIndex={-1}
            {...(inCell && KEYBOARD_OWNER_PROPS)}
            data-pick-surface=""
            data-pick-cell={cellId}
            data-cell-panel={kind}
            data-cell-panel-host={host}
            onKeyDown={onKeyDown}
            className={cn('@container flex flex-col overflow-hidden bg-sheet text-[0.8125rem] text-ink outline-none cursor-default', inCell ? SHEET_IN_CELL : SHEET_IN_PANE)}
        >
            <div className="flex h-9 flex-none items-center gap-1.5 border-b border-line pl-3 pr-2">
                <span className={cn(panelLabel, 'mr-auto min-w-0 truncate')}>{title}</span>
                {step && <button type="button" className={btn} onClick={step.onBack}><ChevronLeft className="h-3.5 w-3.5" aria-hidden />Back</button>}
                <button type="button" className={btn} onClick={() => close('close')}>Close</button>
            </div>
            {step ? <>
                <div ref={stepRef} tabIndex={-1} data-cell-panel-step="" className="flex min-h-0 flex-1 flex-col outline-none">{step.body}</div>
                {confirmBar}
            </> : <>
                {note && <div data-testid="cell-panel-note" className="mx-3 mt-2.5 flex-none rounded-md border border-line bg-sheet-2 px-2.5 py-2 text-[0.78125rem] leading-[1.45] text-ink-2">{note}</div>}
                {/* The hint wraps onto a hidden second line when the tabs need its
                    room: the tab group fills the row's height, so a wrapped
                    hint falls below the row and is clipped whole. */}
                {modes && modes.length > 0 && <div className="mt-2 flex h-[1.875rem] flex-none flex-wrap items-start overflow-hidden border-b border-line pl-0.5 pr-2" onMouseDown={keepFocus}>
                    <div ref={modeGroupRef} role="group" aria-label="Mode" className="relative flex h-[1.8125rem] max-w-full overflow-x-auto [scrollbar-width:none]">
                        {modes.map(m => {
                            const on = m.id === mode;
                            return <button key={m.id} type="button" aria-pressed={on} disabled={m.disabled} title={m.disabled ? m.reason : undefined}
                                onMouseDown={e => e.preventDefault()}
                                onClick={() => { onMode?.(m.id); searchRef.current?.focus({ preventScroll: true }); }}
                                className={cn(modeTab, on ? modeTabOn : modeTabOff, on && (selectedCell ? 'after:bg-brass' : 'after:bg-teal'))}>{m.label}</button>;
                        })}
                    </div>
                    {/* Only while Tab has another mode to go to. */}
                    {modes.filter(m => !m.disabled).length > 1 && <span data-cell-panel-mode-hint="" className="ml-auto mt-px inline-flex h-[1.125rem] flex-none items-center gap-[0.3125rem] whitespace-nowrap pl-3 text-[0.71875rem] text-ink-3"><kbd className={kbd}>tab</kbd> to toggle</span>}
                </div>}
                {location && <div data-testid="cell-panel-location" className="mx-3 mt-2 flex min-w-0 flex-none items-center gap-1.5 overflow-hidden whitespace-nowrap text-[0.71875rem] text-ink-3">{location}</div>}
                {onQuery && <div className="mx-3 mb-1.5 mt-2.5 flex flex-none items-center gap-1.5">
                    <label className="flex h-[1.875rem] min-w-0 flex-1 items-center gap-2 rounded-md border border-line bg-sheet px-2.5 text-ink-3 transition-colors focus-within:border-line-2">
                        <SearchIcon className="h-3.5 w-3.5 flex-none" aria-hidden />
                        {/* The ghost completion lies over the input, so the
                            input keeps one place in the tree and its focus
                            whether a completion shows or not. */}
                        <span className="relative flex h-full min-w-0 flex-1">
                            <input ref={searchRef} type="text" spellCheck={false} autoComplete="off"
                                role="combobox" aria-label={placeholder} aria-controls={rows ? listId : undefined} aria-expanded={!!rows}
                                aria-activedescendant={selected ? optionId(selected.id) : undefined}
                                aria-autocomplete={completion ? 'inline' : undefined}
                                aria-describedby={fieldNote ? fieldNoteId : undefined}
                                placeholder={placeholder} value={query ?? ''} onChange={e => onQuery(e.target.value)}
                                onKeyDown={e => { if (onFieldKey?.(e)) { e.preventDefault(); e.stopPropagation(); } }}
                                onScroll={e => setFieldScroll(e.currentTarget.scrollLeft)}
                                // A completion keeps room at the field's end, so a
                                // name that fills the field still shows it.
                                style={completion ? { paddingRight: `calc(${completion.length}ch + 0.25rem)` } : undefined}
                                className="h-full min-w-0 flex-1 border-0 bg-transparent p-0 text-[0.8125rem] text-ink outline-none placeholder:text-ink-3" />
                            {completion && <span aria-hidden data-filename-completion="" className="pointer-events-none absolute inset-0 flex items-center overflow-hidden text-[0.8125rem]">
                                <span className="whitespace-pre" style={{ transform: `translateX(${-fieldScroll}px)` }}>
                                    <span className="invisible">{query}</span><span className="text-ink-3">{completion}</span>
                                </span>
                            </span>}
                        </span>
                    </label>
                    {searchActions?.map(action => <ActionButton key={action.id} action={action} />)}
                </div>}
                {error && <p role="alert" className="mx-3 mb-1 flex-none text-[0.75rem] text-red">{error}</p>}
                {fieldNote && <p id={fieldNoteId} data-testid="cell-panel-field-note" aria-live="polite" className="mx-3 mb-1 flex-none text-[0.71875rem] text-ink-3">{fieldNote}</p>}
                {rows ? <div ref={listRef} id={listId} role="listbox" aria-label={title} onMouseDown={keepFocus}
                    className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2 pt-0.5">
                    {rows.map(row => {
                        const on = row.id === selectedId;
                        return <div key={row.id} id={optionId(row.id)} role="option" aria-selected={on} aria-disabled={row.disabled || undefined}
                            // A double-click's second press lands on whatever its
                            // first one brought up: with clickRuns it runs nothing.
                            onClick={e => { if (!clickRuns) onSelect?.(row.id); else if (e.detail < 2) runRow(row); }}
                            onDoubleClick={clickRuns ? undefined : () => runRow(row)}
                            className={cn('rounded-md px-2 py-[0.4375rem]', row.disabled ? 'cursor-default' : 'cursor-pointer', on && 'bg-sheet-2')}>
                            <div className={cn('font-medium', row.disabled ? 'text-ink-3' : on ? 'text-ink' : 'text-ink-2')}>{row.name}</div>
                            {row.description && <div className="mt-px text-[0.75rem] leading-[1.4] text-ink-3">{row.description}</div>}
                            {row.note && <div data-testid="cell-panel-row-note" className="mt-px truncate text-[0.71875rem] italic leading-[1.4] text-ink-3">{row.note}</div>}
                            {row.tags && row.tags.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{row.tags.map(tag => <RowTag key={typeof tag === 'string' ? tag : tag.label} tag={tag} />)}</div>}
                            {row.reason && <div className={cn('mt-px text-[0.75rem] leading-[1.4]', row.disabled ? 'text-ink-3' : 'text-red')}>{row.reason}</div>}
                        </div>;
                    })}
                    {rows.length === 0 && empty && <div className="rounded-md px-2 py-[0.4375rem] text-ink-3">{empty}</div>}
                    {rows.length === 0 && emptyAction && <div className="px-2 pb-1.5">
                        <button type="button" data-testid="cell-panel-empty-action" className={btn} onClick={emptyAction.onSelect}>{emptyAction.label}</button>
                    </div>}
                </div> : <div className="flex-1" onMouseDown={keepFocus} />}
                {confirmBar}
                {primary && <div className="flex h-10 flex-none items-center gap-2.5 border-t border-line pl-3 pr-2">
                    {/* A hint without room wraps onto a hidden second line,
                        never clipped mid-word. The empty ::before fills the
                        first line's height, so even the first hint can wrap
                        away. */}
                    <span data-cell-panel-hints="" className="flex h-[1.125rem] min-w-0 flex-[1_1_auto] flex-wrap items-center gap-x-2.5 overflow-hidden whitespace-nowrap text-[0.71875rem] text-ink-3 before:-mr-2.5 before:h-full before:content-[''] @max-[18.75rem]:hidden">
                        {rows && <span className={FOOT_HINT}><kbd className={kbd}>↑↓</kbd> select</span>}
                        {hints?.map(hint => <span key={hint.keys} className={FOOT_HINT}><kbd className={kbd}>{hint.keys}</kbd> {hint.label}</span>)}
                    </span>
                    <div className="flex min-w-0 items-center gap-1.5 @max-[18.75rem]:ml-auto">
                        {secondary && <button type="button" data-testid="cell-panel-secondary" className={btn} aria-label={secondary.label} title={secondary.label}
                            disabled={secondary.disabled} onClick={secondary.onRun}>
                            <span className="@max-[18.75rem]:hidden">{secondary.label}</span><kbd className={cn(kbd, 'ml-0.5 @max-[18.75rem]:ml-0')}>{secondary.hint}</kbd>
                        </button>}
                        {/* A press leaves the keyboard in the field: what the
                            primary turns it into (a new entry's name, a folder's
                            filter) takes the next keys, and a button going
                            disabled under the press never drops them. */}
                        <button type="button" data-testid="cell-panel-primary" className={cn(PRIMARY, PRIMARY_INTENT[primary.intent])}
                            disabled={primary.disabled} title={primary.disabled ? primary.reason : undefined} onMouseDown={e => e.preventDefault()} onClick={runPrimary}>
                            <primary.icon className="h-3.5 w-3.5 flex-none" />
                            <span className="min-w-0 truncate">{primary.label}</span>
                            <kbd className={cn(kbd, 'ml-0.5 font-normal')}>↵</kbd>
                        </button>
                    </div>
                </div>}
            </>}
        </div>
    </>;
}
