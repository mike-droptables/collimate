import type { ExampleDeclaration, ExampleExpect } from '@/lib/definitionApi';
import { formInput, formLabel, formSelect, formTextarea } from '@/components/ui/formStyles';

/** An example's declaration: its description, the verdict the handler's
 *  verifier is expected to give its files, and the feedback it should
 *  produce. The card's Add example step and an added example's card share it. */
export function ExampleFields({ value, onChange, disabled = false }: {
    value: ExampleDeclaration;
    onChange: (next: ExampleDeclaration) => void;
    disabled?: boolean;
}) {
    return <>
        <label className="flex flex-col gap-1">
            <span className={formLabel}>Description</span>
            <input className={formInput} aria-label="Example description" value={value.description} disabled={disabled}
                onChange={e => onChange({ ...value, description: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
            <span className={formLabel}>Expected verdict</span>
            <select className={formSelect} aria-label="Expected verdict" value={value.expect} disabled={disabled}
                onChange={e => onChange({ ...value, expect: e.target.value as ExampleExpect })}>
                <option value="valid">Valid</option>
                <option value="invalid">Invalid</option>
            </select>
        </label>
        <label className="flex flex-col gap-1">
            <span className={formLabel}>Feedback</span>
            <textarea className={formTextarea} aria-label="Example feedback" rows={2} value={value.feedback} disabled={disabled}
                onChange={e => onChange({ ...value, feedback: e.target.value })} />
        </label>
    </>;
}
