import type { SuggestedQuickField } from '@/lib/api/suggested';
import { accountSettings, type Account } from '@/lib/accountsApi';
import { promptGate, type RunnerDraft, type RunnerGenerator } from '@/lib/runnerApi';
import { computeVisibility, effectiveValues, normalizeOptions } from '@/lib/setYaml';
import { FieldControl, disabledOptionValues } from '@/components/runner/FieldControl';
import { RunnerAccountField } from '@/components/runner/RunnerAccountField';

interface Props {
    draft: RunnerDraft;
    generator: RunnerGenerator;
    fields: SuggestedQuickField[];
    accounts: Account[];
    disabled: boolean;
    onChange: (draft: RunnerDraft) => void;
    onOpenKeys: () => void;
}

/** A suggestion exposes existing runner fields, with the same account
 *  picker and conditional setting controls as the full editor. */
export function SuggestedQuickFields({ draft, generator, fields: quickFields, accounts, disabled, onChange, onOpenKeys }: Props) {
    const { runner } = draft;
    const fields = generator.fields.map(field => ({ ...field, options: normalizeOptions(field.options) }));
    const values = effectiveValues({ fields, values: accountSettings(generator, runner.accounts, accounts, runner.settings[generator.main_config] ?? {}) });
    const visible = computeVisibility(fields, values);
    const types = Object.fromEntries(fields.map(field => [field.key, field.type]));
    const update = (patch: Partial<RunnerDraft['runner']>) => onChange({ ...draft, runner: { ...runner, ...patch } });
    const controls = quickFields.map(ref => {
        if ('account' in ref) {
            const role = ref.account;
            const requirement = generator.account_requirements[role];
            if (!requirement) return null;
            return <RunnerAccountField key={`account:${role}`} role={role} requirement={requirement}
                accounts={accounts} value={runner.accounts[role] ?? ''} disabled={disabled} onOpenKeys={onOpenKeys}
                onChange={value => {
                    const bindings = { ...runner.accounts };
                    if (value) bindings[role] = value;
                    else delete bindings[role];
                    update({ accounts: bindings });
                }} />;
        }
        if ('setting' in ref) {
            const field = fields.find(item => item.key === ref.setting);
            if (!field || visible[field.key] === false) return null;
            return <FieldControl key={`setting:${field.key}`} field={field} value={values[field.key]} disabled={disabled}
                disabledOptions={disabledOptionValues(field, values, visible, types)}
                onChange={value => update({ settings: {
                    ...runner.settings,
                    [generator.main_config]: { ...runner.settings[generator.main_config], [field.key]: value },
                } })} />;
        }
        const gate = promptGate(generator, values);
        // What the generator reads from the prompt, as the runner editor shows it.
        return gate.shown ? <div key="prompt" className="flex flex-col gap-1">
            <FieldControl field={{ key: 'prompt', label: 'Prompt', type: 'textarea', required: gate.required }}
                value={runner.prompt} disabled={disabled} onChange={value => update({ prompt: String(value) })} />
            {generator.prompt_intent && <span className="text-[0.71875rem] text-ink-3">{generator.prompt_intent}</span>}
        </div> : null;
    }).filter(Boolean);
    return controls.length ? <div className="flex flex-col gap-3 border-t border-line p-3">{controls}</div> : null;
}
