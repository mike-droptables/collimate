import { requestJson } from './api/client';
import type { RunnerGenerator } from './runnerApi';
import { effectiveValues } from './setYaml';

export interface AccountRequirement {
    label?: string; kind?: string; kinds?: string[]; service?: string; services?: string[]; protocol?: string; protocols?: string[];
    auth_type?: string; auth_types?: string[]; custom_type?: string; custom_types?: string[];
    harness?: string; harnesses?: string[];
    capabilities?: string[]; required?: boolean;
}

export interface Account {
    id: string; name: string; kind: string; service: string; protocol: string;
    endpoint: string; model: string; capabilities: string[]; auth_type: string;
    metadata: Record<string, unknown>; configured: boolean; revision?: string; generation?: string;
    credential_account_id?: string | null; secret_fields?: string[];
    supported_harnesses?: string[]; model_configuration_error?: string;
    /** On every model entry and model-capable account: what a blank model
     *  runs (the SDK taxonomy's `default_model_for`), `null` where the
     *  endpoint has no default and a model must be named. */
    default_model?: string | null;
}

export interface AccountService { id: string; name: string; protocols: string[]; auth_types: string[]; endpoint?: string; kinds?: string[]; capabilities?: string[]; help?: Record<string, string>; auth_labels?: Record<string, string> }

export interface AccountsPayload {
    accounts: Account[]; services: AccountService[];
    /** Per app whose plan login is pasted here: the command that signs in on
     *  this machine, the store that sign-in writes ('' for a login that is a
     *  token the user creates, which no sign-in writes), and the secret the
     *  pasted contents are saved as. */
    /** Each app's login: the command that signs in, the file it writes, the
     *  secret the paste is saved as, the sentence under the field, and
     *  whether the paste is optional (the app's CLI surface signs in on this
     *  machine itself, so only the other surfaces read the document). */
    plan_logins: Record<string, { command: string; store: string; field: 'credential_json' | 'credential_file'; hint: string; optional: boolean }>;
    /** Every harness a model entry can select, from the SDK's account taxonomy. */
    model_harnesses: string[];
    default_model_harness: string;
    harness_labels: Record<string, string>;
    /** Per service with a subscription plan: the plan, and the harness a new
     *  login under it starts on. */
    login_plans: Record<string, { plan: string; harness: string }>;
    status: { available: boolean; config_path: string; error?: string };
}

export const fetchAccounts = () => requestJson<AccountsPayload>('/accounts');

/** The result of testing an unsaved account draft; the instruction for the
 *  credential sits under the Authentication method, so a failure carries
 *  only its message. */
export interface AccountCheck {
    valid: boolean | null; message?: string; supported?: boolean;
    /** An SSH-key Git entry's public key, to add to the server. */
    public_key?: string;
}

export const checkAccountDraft = (body: Record<string, unknown>) => requestJson<AccountCheck>('/accounts/check', 'POST', body);

/** Whether a saved entry can fill a requirement. A model entry with no model
 *  runs the harness's default model, so a blank model is a complete setup; the
 *  server names the one case that has no default — a custom endpoint under an
 *  API key — with model_configuration_error, which disqualifies it here. */
export function accountMatches(account: Account, requirement: AccountRequirement): boolean {
    if (!account.configured) return false;
    if (account.kind === 'model' && (account.model_configuration_error || (account.supported_harnesses && !account.supported_harnesses.includes(String(account.metadata.harness))))) return false;
    const matches = (value: unknown, single?: string, multiple?: string[]) =>
        multiple !== undefined ? typeof value === 'string' && multiple.includes(value) : single === undefined || value === single;
    return matches(account.kind, requirement.kind, requirement.kinds)
        && matches(account.service, requirement.service, requirement.services)
        && matches(account.protocol, requirement.protocol, requirement.protocols)
        && matches(account.auth_type, requirement.auth_type, requirement.auth_types)
        && matches(account.metadata.custom_type, requirement.custom_type, requirement.custom_types)
        && matches(account.metadata.harness, requirement.harness, requirement.harnesses)
        && (requirement.capabilities ?? []).every(cap => account.capabilities.includes(cap));
}


/** The requirement and the selector use exactly the same saved taxonomy. */
export function accountRequirementDescription(requirement: AccountRequirement): string {
    const parts = [
        ['Type', requirement.kinds ?? (requirement.kind ? [requirement.kind] : [])],
        ['Custom type', requirement.custom_types ?? (requirement.custom_type ? [requirement.custom_type] : [])],
        ['Service', requirement.services ?? (requirement.service ? [requirement.service] : [])],
        ['Protocol', requirement.protocols ?? (requirement.protocol ? [requirement.protocol] : [])],
        ['Authentication', requirement.auth_types ?? (requirement.auth_type ? [requirement.auth_type] : [])],
        ['Harness', requirement.harnesses ?? (requirement.harness ? [requirement.harness] : [])],
        ['Capabilities', requirement.capabilities ?? []],
    ] as const;
    return parts.filter(([, values]) => values.length).map(([label, values]) => `${label}: ${values.join(', ')}`).join(' · ');
}


/** How a saved entry reads in a runner dropdown. A model entry always names a
 *  model, and a blank one names the harness default it will run. */
export function accountOptionLabel(account: Account): string {
    const details = [account.name];
    if (account.kind === 'model') details.push(account.model || 'harness default');
    else if (account.model) details.push(account.model);
    if (account.metadata.harness) details.push(String(account.metadata.harness));
    if (account.kind === 'custom' && account.metadata.custom_type) details.push(String(account.metadata.custom_type));
    return details.join(' · ');
}


export function accountSettings(generator: Pick<RunnerGenerator, 'fields' | 'account_requirements'>, bindings: Record<string, string>, accounts: Account[], values: Record<string, unknown>): Record<string, unknown> {
    const result = { ...values };
    const defaults = effectiveValues({ fields: generator.fields, values: {} });
    for (const field of generator.fields) {
        if (!field.account_role || !field.account_property) continue;
        if (!bindings[field.account_role]) {
            result[field.key] = defaults[field.key];
            continue;
        }
        const account = accounts.find(a => a.id === bindings[field.account_role!]);
        if (!account || !accountMatches(account, generator.account_requirements[field.account_role] ?? {})) continue;
        let value: unknown = account;
        for (const key of field.account_property.split('.')) {
            value = value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, key)
                ? (value as Record<string, unknown>)[key] : undefined;
        }
        result[field.key] = value ?? defaults[field.key];
    }
    return result;
}


/** How each authentication method is named. What to paste for it comes from
 *  the service catalog, one sentence per service and method. */
export const AUTH_LABELS: Record<string, string> = {
    api_key: 'API key', basic: 'Username and password', cli: 'App login',
    none: 'No authentication', subscription: 'Claude subscription', ssh_key: 'SSH key',
};

/** A service's own name for a method, from the catalog, wins over the generic one. */
export const authLabel = (auth: string, service?: Pick<AccountService, 'auth_labels'>) =>
    service?.auth_labels?.[auth] ?? AUTH_LABELS[auth] ?? auth.replaceAll('_', ' ');
