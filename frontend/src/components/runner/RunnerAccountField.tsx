import { KeyRound } from 'lucide-react';
import {
    accountMatches, accountOptionLabel, accountRequirementDescription,
    type Account, type AccountRequirement,
} from '@/lib/accountsApi';
import { btn, formSelect, iconBtn, sectionLabel } from '@/components/ui/formStyles';

interface Props {
    role: string;
    requirement: AccountRequirement;
    accounts: Account[];
    value: string;
    disabled: boolean;
    onChange: (value: string) => void;
    onOpenKeys: () => void;
}

/** A runner account role, shared by the full editor and suggested draft setup. */
export function RunnerAccountField({ role, requirement, accounts, value, disabled, onChange, onOpenKeys }: Props) {
    const choices = accounts.filter(account => accountMatches(account, requirement));
    const selectedAccount = choices.find(account => account.id === value);
    const label = requirement.label ?? role;
    const description = accountRequirementDescription(requirement) || 'Any configured account.';
    const grouped = new Map<string, Account[]>();
    for (const account of choices) {
        const parent = account.credential_account_id ?? '';
        grouped.set(parent, [...(grouped.get(parent) ?? []), account]);
    }

    return <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
            <span className="group relative flex">
                <button type="button" aria-label={`${label} requirement`} title={description} disabled={disabled} className={`${iconBtn} h-5 w-5`}><KeyRound className="h-3.5 w-3.5" /></button>
                <span role="tooltip" className="pointer-events-none absolute left-0 top-6 z-10 hidden w-64 rounded-md border border-line-2 bg-sheet p-2 text-[0.75rem] leading-relaxed text-ink-2 shadow-(--shadow) group-hover:block group-focus-within:block">{description}</span>
            </span>
            <span className="text-[0.75rem]">{label}</span>
            <span className={sectionLabel}>{requirement.required !== false ? 'Required' : 'Optional'}</span>
        </div>
        {choices.length || value ? <select className={formSelect} aria-label={label} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
            <option value="">{requirement.required === false ? 'None' : 'Choose an account…'}</option>
            {value && !selectedAccount && <option value={value} disabled>Unavailable account ({value})</option>}
            {Array.from(grouped, ([parent, entries]) => parent ? <optgroup key={parent} label={accounts.find(account => account.id === parent)?.name ?? 'Account unavailable'}>{entries.map(account => <option key={account.id} value={account.id}>{accountOptionLabel(account)}</option>)}</optgroup> : entries.map(account => <option key={account.id} value={account.id}>{accountOptionLabel(account)}</option>))}
        </select> : <div className="flex items-center gap-2"><span className="text-[0.75rem] text-red">No valid account</span><button type="button" className={btn} disabled={disabled} onClick={onOpenKeys}>Open Key manager</button></div>}
        {value && !selectedAccount && <p className="text-[0.75rem] text-red">This entry is missing, incomplete, or no longer compatible. Choose another entry.</p>}
        {selectedAccount?.endpoint && <p className="break-all text-[0.6875rem] font-mono text-ink-3">{selectedAccount.endpoint}</p>}
    </div>;
}
