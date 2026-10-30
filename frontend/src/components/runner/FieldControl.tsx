import React from 'react';
import { cn } from '@/lib/utils';
import { formInput, formLabel, formSelect, formTextarea } from '@/components/ui/formStyles';
import { enabledOptions, matches, type SetYamlField } from '@/lib/setYaml';

/** The option values a dropdown must render DISABLED. Mirrors the in-cell
 *  schema_form renderer (`disabled={!enabledSet.has(opt.value)}`) — with the
 *  three-valued guard this surface needs.
 *
 *  The catalog projects only the EXPOSED keys, and deliberately keeps
 *  conditions whose referents were NOT exposed (a tag may FIX a knob and
 *  expose only the ones downstream of it). Those referents never reach us, so
 *  such a condition is UNEVALUABLE here. `enabledOptions` folds UNKNOWN to
 *  "not enabled" — the right polarity for a *gate*, the wrong one for
 *  *rendering*, where it would make a legitimate choice unreachable. So we
 *  disable only on a DEFINITE false, which `{not: cond}` reports exactly
 *  (Kleene negation keeps UNKNOWN unknown). The server re-validates either way. */
export function disabledOptionValues(
    field: SetYamlField,
    resolved: Record<string, unknown>,
    visible: Record<string, boolean>,
    fieldTypes: Record<string, string | undefined>,
): Set<string> {
    const out = new Set<string>();
    if (field.type !== 'dropdown') return out;
    const enabled = new Set(enabledOptions(field, resolved, visible, fieldTypes));
    for (const opt of field.options ?? []) {
        if (!opt.enable_if || enabled.has(opt.value)) continue;
        if (matches({ not: opt.enable_if }, resolved, visible, fieldTypes)) out.add(opt.value);
    }
    return out;
}

export interface FieldControlProps {
    field: SetYamlField;
    value: unknown;
    disabled: boolean;
    /** Dropdown option values whose `enable_if` is DEFINITELY unmet — rendered
     *  greyed out but still present (see disabledOptionValues). */
    disabledOptions?: ReadonlySet<string>;
    onChange: (value: unknown) => void;
}

/**
 * One exposed `.set_yaml` field rendered as a controlled input. A simple
 * form over the exposed subset — text/textarea/number/checkbox/dropdown get
 * controls here; list/object fields are rendered by RunnerEditor as JSON
 * textareas and never reach this component.
 *
 * The RunnerEditor's settings form renders one of these per field of the
 * generator's main config.
 */
export function FieldControl({ field, value, disabled, disabledOptions, onChange }: FieldControlProps) {
    const label = field.label || field.key;
    const labelEl = (
        <span className={cn(formLabel, 'font-mono')}>
            {label}
            {field.required && <span className="text-red"> *</span>}
        </span>
    );

    if (field.type === 'checkbox') {
        return (
            <label className="flex items-center gap-2 cursor-pointer">
                <input
                    type="checkbox"
                    checked={value === true || value === 'true' || value === 1}
                    disabled={disabled}
                    onChange={e => onChange(e.target.checked)}
                    className="accent-teal"
                />
                {labelEl}
            </label>
        );
    }

    if (field.type === 'dropdown') {
        const cur = value == null ? '' : String(value);
        return (
            <label className="flex flex-col gap-1">
                {labelEl}
                <select
                    value={cur}
                    disabled={disabled}
                    onChange={e => onChange(e.target.value)}
                    className={cn(formSelect, 'font-mono')}
                >
                    <option value="" disabled hidden>—</option>
                    {(field.options ?? []).map(opt => (
                        <option
                            key={opt.value}
                            value={opt.value}
                            title={opt.description}
                            disabled={disabledOptions?.has(opt.value) ?? false}
                        >
                            {opt.label}
                        </option>
                    ))}
                </select>
            </label>
        );
    }

    if (field.type === 'number') {
        return (
            <label className="flex flex-col gap-1">
                {labelEl}
                <input
                    type="number"
                    value={value == null ? '' : String(value)}
                    min={field.min}
                    max={field.max}
                    disabled={disabled}
                    onChange={e => onChange(e.target.value === '' ? null : Number(e.target.value))}
                    className={cn(formInput, 'font-mono')}
                />
            </label>
        );
    }

    if (field.type === 'textarea') {
        return (
            <label className="flex flex-col gap-1">
                {labelEl}
                <textarea
                    value={value == null ? '' : String(value)}
                    disabled={disabled}
                    rows={4}
                    onChange={e => onChange(e.target.value)}
                    className={cn(formTextarea, 'resize-none font-mono')}
                />
            </label>
        );
    }

    // text (default)
    return (
        <label className="flex flex-col gap-1">
            {labelEl}
            <input
                type="text"
                value={value == null ? '' : String(value)}
                disabled={disabled}
                onChange={e => onChange(e.target.value)}
                className={cn(formInput, 'font-mono')}
            />
        </label>
    );
}
