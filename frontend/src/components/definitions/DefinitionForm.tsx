import { useState, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { definitionText, parseDefinition, type DefinitionKind } from '@/lib/definitionFiles';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { Switch } from '@/components/ui/switch';
import { btn, btnPrimary, card, chip, formInput, formLabel, formSelect, formTextarea, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** A field's frame: its key as the label over the control. */
const FIELD = 'flex min-w-0 flex-col gap-1';
const MONO_AREA = `${formTextarea} font-mono text-[0.75rem]`;
const UNSAFE_KEYS = ['__proto__', 'prototype', 'constructor'];

const isStringList = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string');
/** A handler's or renderer's `types`: a list of `{ ext, … }` records. */
const typeExts = (value: unknown): string[] => Array.isArray(value)
    ? value.flatMap(item => item && typeof item === 'object' && typeof (item as { ext?: unknown }).ext === 'string' ? [(item as { ext: string }).ext] : [])
    : [];

/**
 * The app-owned form over a definition file (`.handler`, `.renderer`,
 * `.generator`, `.options_stack`): one field per top-level key, on the
 * shared form vocabulary. Lists of extensions are chips, `types` shows its
 * extensions as chips over the record editor, a structured value is a JSON
 * editor, and a handler's `examples` are one card per example (folder,
 * description, expected verdict, feedback). Save writes the whole mapping
 * back; malformed YAML refuses to save rather than overwrite it.
 */
export function DefinitionForm({ content, readOnly, onSave, kind, children }: {
    content: string;
    readOnly: boolean;
    onSave: (text: string) => void | Promise<void>;
    /** The definition's kind: a handler's examples get their own editor. */
    kind?: DefinitionKind | null;
    children?: ReactNode;
}) {
    const [values, setValues] = useState<Record<string, unknown>>({});
    const [structured, setStructured] = useState<Record<string, string>>({});
    const [error, setError] = useState('');
    const [parseValid, setParseValid] = useState(false);
    const [busy, setBusy] = useState(false);
    const [newField, setNewField] = useState('');
    const [extension, setExtension] = useState('');
    // A new body re-reads the form (and drops unsaved edits).
    const [parsed, setParsed] = useState<string | null>(null);
    if (parsed !== content) {
        setParsed(content); setStructured({});
        try { setValues(parseDefinition(content)); setError(''); setParseValid(true); }
        catch (e) { setValues({}); setParseValid(false); setError(e instanceof Error ? e.message : String(e)); }
    }
    const save = async () => {
        if (!parseValid || readOnly || busy) return;
        setBusy(true); setError('');
        try {
            const next = { ...values };
            for (const [key, value] of Object.entries(structured)) next[key] = JSON.parse(value);
            await onSave(definitionText(next));
        } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
        finally { setBusy(false); }
    };
    const set = (key: string, value: unknown) => setValues({ ...values, [key]: value });
    const addExtension = (list: string[]) => {
        const item = extension.trim();
        if (item && !list.includes(item)) set('extensions', [...list, item]);
        setExtension('');
    };
    const addField = () => {
        const key = newField.trim();
        if (key && !Object.hasOwn(values, key) && !UNSAFE_KEYS.includes(key)) set(key, '');
        setNewField('');
    };

    const field = (key: string, value: unknown): ReactNode => {
        if (kind === 'handler' && key === 'examples' && Array.isArray(value)) {
            return <HandlerExamples key={key} examples={value} onChange={next => set(key, next)} />;
        }
        if (key === 'extensions' && isStringList(value)) {
            return <div key={key} className={FIELD}>
                <span className={formLabel}>{key}</span>
                <div className="flex flex-wrap items-center gap-1.5">
                    {value.map(item => <button type="button" key={item} aria-label={`Remove extension ${item}`}
                        className={cn(chip, 'font-mono transition-colors hover:bg-sheet-2 hover:text-ink')}
                        onClick={() => set('extensions', value.filter(entry => entry !== item))}>
                        {item}<X className="h-3 w-3 text-ink-3" aria-hidden />
                    </button>)}
                    <input aria-label="Add extension" placeholder=".ext" className={cn(formInput, 'w-24 font-mono text-[0.75rem]')}
                        value={extension} onChange={e => setExtension(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addExtension(value); } }} />
                    <button type="button" className={btn} onClick={() => addExtension(value)}>Add extension</button>
                </div>
            </div>;
        }
        if (typeof value === 'boolean') {
            return <div key={key} className="flex items-center justify-between gap-3">
                <span className={formLabel}>{key}</span>
                <Switch aria-label={key} checked={value} onCheckedChange={next => set(key, next)} />
            </div>;
        }
        if (value !== null && typeof value === 'object') {
            const exts = key === 'types' ? typeExts(value) : [];
            return <label key={key} className={FIELD}>
                <span className={formLabel}>{key}</span>
                {exts.length > 0 && <span className="flex flex-wrap gap-1">{exts.map(ext => <span key={ext} className={cn(chip, 'font-mono')}>{ext}</span>)}</span>}
                <textarea aria-label={key} className={MONO_AREA} rows={4}
                    value={structured[key] ?? JSON.stringify(value, null, 2)}
                    onChange={e => setStructured({ ...structured, [key]: e.target.value })} />
            </label>;
        }
        return <label key={key} className={FIELD}>
            <span className={formLabel}>{key}</span>
            <input aria-label={key} className={formInput} value={value == null ? '' : String(value)}
                onChange={e => set(key, typeof value === 'number' ? Number(e.target.value) : e.target.value)} />
        </label>;
    };

    return <div className="flex h-full min-h-0 flex-1 flex-col gap-3 overflow-auto p-3 text-[0.8125rem] text-ink" {...KEYBOARD_OWNER_PROPS}>
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        <fieldset disabled={readOnly || busy} className="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
            {Object.entries(values).map(([key, value]) => field(key, value))}
            <div className="flex items-center gap-2">
                <input aria-label="Additional definition field" placeholder="New field" className={formInput}
                    value={newField} onChange={e => setNewField(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addField(); } }} />
                <button type="button" className={btn} onClick={addField}>Add field</button>
            </div>
            <div className="flex items-center gap-2 border-t border-line pt-3">
                <button type="button" className={btnPrimary} onClick={() => void save()} disabled={readOnly || busy || !parseValid}>Save definition</button>
            </div>
        </fieldset>
        {children}
    </div>;
}

/** A handler's examples: one card per declared example folder. A malformed
 *  entry is named and left for the source editor to repair. */
function HandlerExamples({ examples, onChange }: { examples: unknown[]; onChange: (next: unknown[]) => void }) {
    return <section className="flex flex-col gap-2">
        <h3 className={sectionLabel}>Examples and feedback</h3>
        {examples.length === 0 && <p className="text-[0.78125rem] text-ink-3">No examples declared.</p>}
        {examples.map((entry, index) => {
            if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
                return <p key={index} className="text-[0.78125rem] text-red">Invalid example {index + 1}; edit the definition source to repair it.</p>;
            }
            const row = entry as Record<string, unknown>;
            const text = (key: string) => typeof row[key] === 'string' ? row[key] as string : '';
            const edit = (key: string, next: string) => onChange(examples.map((item, i) => i === index ? { ...row, [key]: next } : item));
            return <fieldset key={index} className={cn(card, 'm-0 flex min-w-0 flex-col gap-2 p-3')}>
                <legend className={cn(formLabel, 'float-left mb-0.5 w-full font-medium text-ink')}>Example {index + 1}</legend>
                <div className="grid grid-cols-2 gap-2">
                    {(['folder', 'description'] as const).map(key => <label key={key} className={FIELD}>
                        <span className={formLabel}>{key}</span>
                        <input aria-label={`Example ${key} ${index + 1}`} className={cn(formInput, key === 'folder' && 'font-mono text-[0.75rem]')}
                            value={text(key)} onChange={e => edit(key, e.target.value)} />
                    </label>)}
                </div>
                <label className={FIELD}>
                    <span className={formLabel}>Expected verdict</span>
                    <select aria-label={`Example expectation ${index + 1}`} className={formSelect} value={text('expect')} onChange={e => edit('expect', e.target.value)}>
                        <option value="">Choose verdict</option>
                        <option value="valid">Valid</option>
                        <option value="invalid">Invalid</option>
                    </select>
                </label>
                <label className={FIELD}>
                    <span className={formLabel}>Feedback</span>
                    <textarea aria-label={`Example feedback ${index + 1}`} className={formTextarea} rows={2} value={text('feedback')} onChange={e => edit('feedback', e.target.value)} />
                </label>
            </fieldset>;
        })}
    </section>;
}
