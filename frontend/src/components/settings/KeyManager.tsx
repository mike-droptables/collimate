import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Trash2, X } from 'lucide-react';
import {
    authLabel, checkAccountDraft, fetchAccounts,
    type Account, type AccountCheck, type AccountService, type AccountsPayload,
} from '@/lib/accountsApi';
import { requestJson } from '@/lib/api/client';
import { runnerSlug } from '@/lib/runnerApi';
import { btn, btnDanger, btnPrimary, card, chip, confirmStrip, dialog, formInput, formLabel, formSelect, formTextarea, iconBtn, sectionLabel, segmentOff, segmentOn } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { basename } from '@/lib/fileTransfer';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { trapModalKeys, useModalFocus } from '@/lib/modalKeys';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';

const blank = (): Account => ({
    id: '',
    name: '',
    kind: 'account',
    service: '',
    protocol: 'openai',
    endpoint: '',
    model: '',
    capabilities: ['text'],
    auth_type: 'api_key',
    metadata: {},
    configured: false
});
interface ModelChoice { id: string; name: string; capabilities?: string[] }
interface CustomField { name: string; secret: boolean; value: string; savedSecretName?: string }
interface ModelDiscovery { models: ModelChoice[]; account?: Account; output?: string; message?: string }

/** Every dropdown reads alphabetically by the label the user sees; the current
 *  value keeps its place in that order. */
const byLabel = (options: { value: string; label: string }[]) => [...options].sort((a, b) => a.label.localeCompare(b.label));
const serviceLabel = (service: AccountService) => service.id === 'custom' ? 'Custom model endpoint' : service.name;
const cachedModels = (account: Account): ModelChoice[] => {
    const cached = account.metadata.model_discovery as { models?: ModelChoice[] } | undefined;
    return Array.isArray(cached?.models) ? cached.models : [];
};
const customFields = (account: Account): CustomField[] => {
    const schema = account.metadata.field_schema as { name: string; secret: boolean }[] | undefined;
    const values = (account.metadata.fields ?? {}) as Record<string, unknown>;
    return (Array.isArray(schema) ? schema : []).map(field => ({
        ...field,
        value: field.secret ? '' : String(values[field.name] ?? ''),
        savedSecretName: field.secret && account.secret_fields?.includes(field.name) ? field.name : undefined
    }));
};

/** A Git entry's commit author email, as the server checks it. */
const EMAIL = /^[^@\s<>]+@[^@\s<>]+$/;
/** What a Git entry's address is called on each Git service, and what it is. */
const GIT_ADDRESS: Record<string, { label: string; hint: string; placeholder: string }> = {
    github: { label: 'Server URL', placeholder: 'https://github.com', hint: 'https://github.com, or your GitHub Enterprise server\'s address (where its repositories open in a browser)' },
    gitlab: { label: 'GitLab URL', placeholder: 'https://gitlab.com', hint: 'https://gitlab.com, or your instance\'s address with its path if it has one' },
    git: { label: 'Server URL', placeholder: 'https://git.example.org', hint: 'The username and token are sent only to repositories under this address' },
};

/** Mirrors MODEL_API_PROTOCOLS in collimate's account_taxonomy.py; used only
 *  for an account whose service is not in the catalog. */
const MODEL_PROTOCOLS = ['openai', 'openai-compatible', 'openai-chat', 'openai-responses', 'anthropic', 'gemini'];

/** Reapply the user's edits while retaining fields changed by the server.
 *  A concurrent edit to the same field needs the user to resolve it. */
function mergeAccountEdits(base: unknown, edited: unknown, current: unknown): unknown {
    const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
    if (equal(edited, base)) return current;
    if (equal(current, base) || equal(current, edited)) return edited;
    const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
    if (record(base) && record(edited) && record(current)) {
        return Object.fromEntries([...new Set([...Object.keys(base), ...Object.keys(edited), ...Object.keys(current)])]
            .map(key => [key, mergeAccountEdits(base[key], edited[key], current[key])])
            .filter(([, value]) => value !== undefined));
    }
    throw new Error('The same field changed in another editor. Reopen the account to compare your edits.');
}

/** Global account metadata, with write-only secrets. No channel is required.
 *  Three views: the account list, the add card that fixes an account's type
 *  and service, and the detail form for one entry. One column throughout, so
 *  it reads the same in the Commands pane's settings view and in the
 *  overlay: every field sits under its label and every row of buttons wraps. */
export function KeyManager() {
    const [data, setData] = useState<AccountsPayload | null>(null);
    const [view, setView] = useState<'list' | 'add' | 'detail'>('list');
    const [addType, setAddType] = useState<'supported' | 'custom' | ''>('');
    const [addService, setAddService] = useState('');
    const [editing, setEditing] = useState<Account | null>(null);
    const [existing, setExisting] = useState(false);
    const [secrets, setSecrets] = useState<Record<string, string>>({});
    const [models, setModels] = useState<ModelChoice[]>([]);
    const [fields, setFields] = useState<CustomField[]>([]);
    const [check, setCheck] = useState<AccountCheck | null>(null);
    const [discoveryTarget, setDiscoveryTarget] = useState<string | null>(null);
    const discovering = discoveryTarget !== null && discoveryTarget === editing?.id;
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState('');
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
    const refresh = async () => {
        const next = await fetchAccounts();
        setData(next);
        return next;
    };
    useEffect(() => {
        let live = true;
        void fetchAccounts().then(d => { if (live) setData(d); }).catch(e => { if (live) setError(e.message); });
        return () => { live = false; };
    }, []);
    /** Every edit to the draft invalidates the last Verify — a stale result
     *  would vouch for credentials, an endpoint or a model that changed. */
    const patch = (value: Partial<Account>) => {
        setCheck(null);
        setEditing(prev => prev ? { ...prev, ...value } : null);
    };
    const editFields = (next: CustomField[]) => {
        setCheck(null);
        setFields(next);
    };
    const editingId = useRef(editing?.id);
    const editingBase = useRef<Account | null>(null);
    const edit = (account: Account, saved: boolean) => {
        editingId.current = account.id;
        editingBase.current = structuredClone(account);
        setEditing({ ...account, metadata: { ...account.metadata } });
        setExisting(saved);
        setSecrets({});
        const owner = data?.accounts.find(item => item.id === account.credential_account_id) ?? account;
        setModels(cachedModels(owner));
        setFields(customFields(account));
        setView('detail');
        setNotice('');
        setError(null);
        setCheck(null);
        setConfirmDelete(null);
    };
    const toList = () => {
        editingId.current = undefined;
        setEditing(null);
        setSecrets({});
        setFields([]);
        setCheck(null);
        setConfirmDelete(null);
        setView('list');
    };
    useEffect(() => { editingId.current = editing?.id; }, [editing?.id]);
    const work = async (fn: () => Promise<void>) => {
        setBusy(true);
        setError(null);
        setNotice('');
        try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : 'Account operation failed'); } finally { setBusy(false); }
    };
    /** The metadata and keyring overlay a save or a check carries. Throws on a
     *  custom field set the keyring cannot represent. */
    const compose = (account: Account) => {
        let metadata = account.metadata;
        const secretChanges: Record<string, string | null> = Object.fromEntries(Object.entries(secrets).filter(([, value]) => value.length > 0));
        if (account.kind === 'custom') {
            const names = fields.map(field => field.name.trim());
            if (names.some(name => !name) || new Set(names).size !== names.length) throw new Error('Give every custom field a unique name.');
            if (names.some(name => !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name))) throw new Error('Field names must start with a letter and use up to 64 letters, numbers or underscores.');
            metadata = {
                ...metadata,
                field_schema: fields.map(field => ({ name: field.name.trim(), secret: field.secret })),
                fields: Object.fromEntries(fields.filter(field => !field.secret).map(field => [field.name.trim(), field.value]))
            };
            for (const name of account.secret_fields ?? []) {
                if (!fields.some(field => field.name.trim() === name && field.secret)) secretChanges[name] = null;
            }
            for (const field of fields) {
                if (field.secret && field.value) secretChanges[field.name.trim()] = field.value;
                if (field.secret && !field.value && field.savedSecretName !== field.name.trim()) throw new Error(`Enter a secret value for ${field.name.trim()}. Saved secrets must be entered again when a field is renamed.`);
            }
        }
        return { metadata, secretChanges };
    };
    /** Capabilities belong to model entries; every other account carries its
     *  service's, derived by the server. */
    const body = (account: Account, metadata: Record<string, unknown>, id?: string) => ({
        ...(id ? { id } : {}),
        name: account.name,
        kind: account.kind,
        service: account.service,
        protocol: account.protocol,
        endpoint: account.endpoint,
        model: account.model,
        auth_type: account.auth_type,
        metadata,
        credential_account_id: account.credential_account_id ?? null,
        ...(account.kind === 'model' ? { capabilities: account.capabilities.filter(Boolean) } : {}),
    });
    const save = () => work(async () => {
        if (!editing) return;
        const id = editing.id || `${runnerSlug(editing.name)}-${crypto.randomUUID().slice(0, 8)}`;
        const { metadata, secretChanges } = compose(editing);
        const path = `/accounts${existing ? `/${encodeURIComponent(id)}` : ''}`;
        const draft = body(editing, metadata, id);
        let saved: Account;
        try {
            saved = await requestJson<Account>(path, existing ? 'PUT' : 'POST', {
                ...draft, ...(existing ? { revision: editing.revision } : {}), secrets: secretChanges,
            });
        } catch (error) {
            if (!existing || !(error instanceof Error) || !('status' in error) || error.status !== 409 || !editingBase.current) throw error;
            const current = (await refresh()).accounts.find(account => account.id === id);
            if (!current) throw new Error('This account was deleted. Reopen Key manager to continue.', { cause: error });
            const base = editingBase.current;
            if (Object.keys(secretChanges).length && (!base.generation || current.generation !== base.generation)) {
                throw new Error('Credentials changed in another editor. Reopen the account before replacing them.', { cause: error });
            }
            const merged = mergeAccountEdits(body(base, base.metadata, id), draft, body(current, current.metadata, id)) as typeof draft;
            saved = await requestJson<Account>(path, 'PUT', { ...merged, revision: current.revision, secrets: secretChanges });
        }
        // The account exists the moment the request returns. Commit it here so
        // a refresh that fails leaves the editor on the SAVED entry — a retry
        // updates it instead of creating a second one.
        const committed = saved?.id ? saved : { ...editing, id, metadata };
        editingBase.current = structuredClone(committed);
        setEditing(committed);
        setExisting(true);
        setSecrets({});
        setFields(customFields(committed));
        await refresh();
        toList();
    });
    /** The draft as the server reads it: its fields and the secrets typed so far. */
    const draftBody = (account: Account) => {
        const { metadata, secretChanges } = compose(account);
        const secrets = Object.fromEntries(Object.entries(secretChanges).filter(([, value]) => typeof value === 'string'));
        return { ...body(account, metadata, existing ? account.id : undefined), secrets };
    };
    const verify = () => work(async () => {
        if (!editing) return;
        setCheck(await checkAccountDraft(draftBody(editing)));
    });
    const remove = (id: string) => work(async () => {
        await requestJson(`/accounts/${encodeURIComponent(id)}`, 'DELETE');
        await refresh();
        setConfirmDelete(null);
    });
    const discover = async (account: Account, targetId: string) => {
        setDiscoveryTarget(targetId);
        try {
            const result = await requestJson<ModelDiscovery>(`/accounts/${encodeURIComponent(account.id)}/models`, 'POST');
            if (result.account) {
                const updated = result.account;
                setData(prev => prev ? { ...prev, accounts: prev.accounts.map(item => item.id === updated.id ? updated : item) } : prev);
                setEditing(previous => {
                    if (previous?.id !== updated.id || previous.revision !== account.revision) return previous;
                    if (JSON.stringify(previous) === JSON.stringify(account)) return updated;
                    // A user can return to the parent while its child's automatic discovery is pending.
                    // Rebase the cache revision while keeping the user's account edits intact.
                    return {
                        ...previous,
                        revision: updated.revision,
                        metadata: { ...previous.metadata, model_discovery: updated.metadata.model_discovery }
                    };
                });
            }
            if (editingId.current !== targetId) return;
            setModels(result.models);
            setNotice(result.output || result.message || (result.models.length ? `${result.models.length} models available. Search the model list or enter a model ID.` : 'No model list is available. Enter a model ID manually.'));
        } catch (e) {
            if (editingId.current === targetId) setNotice(`Model discovery unavailable: ${e instanceof Error ? e.message : 'service did not respond'}. Enter a model ID manually.`);
        } finally { setDiscoveryTarget(current => current === targetId ? null : current); }
    };
    const loadModels = () => work(async () => {
        if (!editing) return;
        const owner = data?.accounts.find(account => account.id === editing.credential_account_id) ?? editing;
        await discover(owner, editing.id);
    });
    const savedAccount = data?.accounts.find(account => account.id === editing?.id);
    const dirty = existing && (JSON.stringify(editing) !== JSON.stringify(savedAccount) || Object.values(secrets).some(Boolean) || (editing?.kind === 'custom' && JSON.stringify(fields) !== JSON.stringify(customFields(savedAccount ?? editing))));
    const service = data?.services.find(s => s.id === editing?.service);
    const isChild = Boolean(editing?.credential_account_id);
    const parent = data?.accounts.find(account => account.id === editing?.credential_account_id);
    const inheritsHarness = Boolean(parent && (parent.protocol === 'cli' || ['cli', 'subscription'].includes(parent.auth_type)));
    /** The harness and plan facts come from the SDK's account taxonomy
     *  through the API: every harness a model entry can select, and per
     *  service the app and subscription its plan credentials come from (for
     *  both an app login and the Claude subscription token). */
    const harnessLabel = (harness: string) => data?.harness_labels[harness] ?? harness;
    const modelHarnesses = data?.model_harnesses ?? [];
    const loginPlans = data?.login_plans ?? {};
    const plans = Array.from(new Set(Object.values(loginPlans).map(row => row.plan))).sort();
    const selectedHarness = String(editing?.metadata.harness || data?.default_model_harness || '');
    const harnessOptions = parent?.supported_harnesses ?? editing?.supported_harnesses ?? modelHarnesses;
    const invalidHarness = editing?.kind === 'model' && !harnessOptions.includes(selectedHarness);
    /** A blank model runs the harness's default model. The API serves, on
     *  the saved account a model entry draws its connection from, what that
     *  default is; where it has none (a custom endpoint under an API key) the
     *  entry must name a model. */
    const credentials = parent ?? editing;
    const modelRequired = editing?.kind === 'model' && credentials?.default_model === null;
    const configurationError = editing?.model_configuration_error;
    const authTypes = service?.auth_types ?? ['api_key', 'basic', 'none'];
    /** The apps whose login document can be pasted here, and the store each one
     *  writes on this machine. */
    const planLogins = data?.plan_logins ?? {};
    const planLogin = planLogins[String(editing?.metadata.harness ?? '')];
    // A login no sign-in writes (Copilot's) is a token the user creates; every
    // other app's sign-in writes a file whose contents are pasted.
    const loginWhat = planLogin && !planLogin.store ? 'token' : 'document';
    const modelDefaults = (account: Account) => {
        const defaults = data?.services.find(item => item.id === account.service)?.capabilities;
        return account.kind !== 'model' && account.capabilities.length ? [...account.capabilities] : defaults?.length ? [...defaults] : ['text'];
    };
    const supportsModels = (account: Account) => {
        if (account.credential_account_id) return false;
        const catalog = data?.services.find(item => item.id === account.service);
        return account.kind === 'model' || (account.kind === 'account' && (catalog?.kinds?.includes('model') || (!catalog?.kinds && MODEL_PROTOCOLS.includes(account.protocol))));
    };
    const addChild = (account: Account) => {
        const supported = account.supported_harnesses ?? modelHarnesses;
        const inherited = String(account.metadata.harness ?? '');
        const harness = supported.includes(inherited) ? inherited : supported[0] ?? data!.default_model_harness;
        const child: Account = {
            ...blank(),
            id: `model-${crypto.randomUUID().slice(0, 8)}`,
            kind: 'model',
            credential_account_id: account.id,
            service: account.service,
            protocol: account.protocol,
            endpoint: account.endpoint,
            auth_type: account.auth_type,
            capabilities: modelDefaults(account),
            metadata: { harness }
        };
        edit(child, false);
        if (!cachedModels(account).length) void discover(account, child.id);
    };
    const startDetail = () => {
        const chosen = data?.services.find(item => item.id === (addType === 'custom' ? 'custom' : addService));
        const kind = addType === 'custom' ? 'custom' : chosen?.kinds?.includes('account') ? 'account' : chosen?.kinds?.[0] ?? 'account';
        // A custom entry's credentials are its secret custom fields, so it carries no auth method.
        const auth = kind === 'custom' ? 'none' : byLabel((chosen?.auth_types ?? ['api_key']).map(value => ({ value, label: authLabel(value, chosen) })))[0].value;
        edit({
            ...blank(),
            kind,
            service: addType === 'custom' ? 'custom' : addService,
            protocol: addType === 'custom' ? 'custom' : chosen?.protocols[0] ?? 'openai',
            endpoint: addType === 'custom' ? '' : chosen?.endpoint ?? '',
            auth_type: auth,
            metadata: ['cli', 'subscription'].includes(auth) && loginPlans[addService] ? { ...loginPlans[addService] } : {},
        }, false);
    };
    /** A plan method carries the app and subscription its credentials belong
     *  to. The Claude subscription is fixed to the app that mints its token; an
     *  app login only fills what the user has not chosen for themselves. */
    const selectAuthentication = (auth: string) => {
        if (!editing) return;
        const defaults = loginPlans[editing.service];
        const metadata = !defaults ? editing.metadata
            : auth === 'subscription' ? { ...editing.metadata, ...defaults }
                : auth === 'cli' ? {
                    ...editing.metadata,
                    harness: editing.metadata.harness || defaults.harness,
                    plan: editing.metadata.plan || defaults.plan
                }
                    : editing.metadata;
        // A Git entry keeps only what its method shows: the username is basic's,
        // the known hosts ssh_key's, and a Git server's URL is basic's too.
        const shown = editing.kind !== 'git' ? metadata : Object.fromEntries(Object.entries(metadata).filter(([key]) =>
            !(key === 'username' && auth !== 'basic') && !(key === 'known_hosts' && auth !== 'ssh_key')));
        patch({ auth_type: auth, metadata: shown, ...(editing.kind === 'git' && editing.service === 'git' && auth !== 'basic' ? { endpoint: '' } : {}) });
        setSecrets({});
    };
    /** What the catalog says to paste for this service and method — the one
     *  instruction the form gives. */
    const authHelp = editing ? service?.help?.[editing.auth_type] : undefined;
    /** Every typed secret is write-only: a stored one is never read back, so an
     *  empty box keeps what is saved. */
    const secretInput = (label: string, key: string) => <label className="flex flex-col gap-1">
        <span className={formLabel}>
            {label}
        </span>
        <input
            aria-label={label}
            className={formInput}
            type="password"
            autoComplete="new-password"
            value={secrets[key] ?? ''}
            placeholder={existing ? 'Leave blank to keep saved secret' : ''}
            onChange={e => {
                setSecrets({ ...secrets, [key]: e.target.value });
                setCheck(null);
            }}
        />
    </label>;
    /** A private key is pasted whole, so its box is a monospace text area;
     *  like every secret it is write-only and only ever shows what was typed. */
    const secretTextarea = (label: string, key: string) => <label className="flex flex-col gap-1">
        <span className={formLabel}>
            {label}
        </span>
        <textarea
            aria-label={label}
            className={cn(formTextarea, 'font-mono')}
            rows={6}
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            value={secrets[key] ?? ''}
            placeholder={editing?.secret_fields?.includes(key) ? 'Leave blank to keep the saved key' : '-----BEGIN OPENSSH PRIVATE KEY-----'}
            onChange={e => {
                setSecrets({ ...secrets, [key]: e.target.value });
                setCheck(null);
            }}
        />
    </label>;
    /** A service the catalog has no endpoint for points at a server the user
     *  runs, so its URL is a main field instead of an advanced override. Plan
     *  services reach their provider through the app and need none. A Git
     *  entry's address is always in the body (where its repositories open),
     *  except a Git server entry's without a username and token, which has none. */
    const endpointInBody = !!editing && !isChild && editing.kind !== 'custom' && !!service && service.protocols[0] !== 'cli'
        && (editing.kind === 'git' ? !(editing.service === 'git' && editing.auth_type !== 'basic') : !service.endpoint);
    const gitAddress = editing?.kind === 'git' ? GIT_ADDRESS[editing.service] : undefined;
    const atDefaultHost = !!service?.endpoint && (editing?.endpoint.replace(/\/+$/, '') || service.endpoint) === service.endpoint;
    const authorName = String(editing?.metadata.author_name ?? '');
    const authorEmail = String(editing?.metadata.author_email ?? '');
    const gitIncomplete = editing?.kind === 'git' && (!authorName.trim() || !EMAIL.test(authorEmail)
        || (editing.service === 'git' && editing.auth_type === 'basic' && !editing.endpoint.trim()));
    const strip = editing ? editing.kind === 'model' ? `Model · ${parent?.name ?? editing.credential_account_id} · ${service ? serviceLabel(service) : editing.service}`
        : editing.kind === 'custom' ? 'Custom'
            : `Supported service · ${service ? serviceLabel(service) : editing.service}` : '';
    const deleteConfirm = (target: Account, label: string) => <div className={confirmStrip}>
        <span className="min-w-0 flex-1">Delete “{target.name}”? {label}
        </span>
        <button className={btnDanger} onClick={() => void remove(target.id)}>Delete</button>
        <button className={btn} onClick={() => setConfirmDelete(null)}>Keep</button>
    </div>;
    const trash = (target: Account) => <button
        type="button"
        className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem] hover:bg-red-soft hover:text-red')}
        aria-label={`Delete ${target.name}`}
        onClick={() => setConfirmDelete(target.id)}
    >
        <Trash2 className="h-3.5 w-3.5" />
    </button>;
    const editControl = (target: Account) => <button
        type="button"
        className={btn}
        aria-label={`Edit ${target.name}`}
        onClick={() => edit(target, true)}
    >Edit</button>;
    const setupFlag = (target: Account) => !target.configured && <span
        className={cn(chip, 'border-teal/40 bg-teal-soft text-teal')}
    >Setup incomplete</span>;

    return <div className="flex flex-col gap-3 text-ink">
        {data?.status.error && <div role="alert" className="rounded-md border border-red/40 bg-red-soft p-3 text-[0.75rem] text-red">Key manager config could not be parsed: {data.status.error}
            <div className="mt-1 break-all text-[0.6875rem] font-mono">
                {data.status.config_path}
            </div>
        </div>}
        {data && !data.status.available && <p role="status" className="text-[0.75rem] text-red">The system keyring is unavailable.</p>}
        {error && <p
            role="alert"
            className="rounded-md border border-red/40 bg-red-soft p-2.5 text-[0.75rem] text-red"
        >
            {error}
        </p>}
        {notice && <p
            role="status"
            className="whitespace-pre-wrap rounded-md border border-line bg-sheet-2 p-2.5 text-[0.75rem] text-ink-2"
        >
            {notice}
        </p>}

        {view === 'list' && <>
            <div className="flex flex-wrap items-center justify-between gap-2">
                <span className={sectionLabel}>Accounts</span>
                <button
                    className={btnPrimary}
                    onClick={() => {
                        setAddType('');
                        setAddService('');
                        setError(null);
                        setNotice('');
                        setConfirmDelete(null);
                        setView('add');
                    }}
                >Add account</button>
            </div>
            {data?.accounts.filter(account => !account.credential_account_id || !data.accounts.some(owner => owner.id === account.credential_account_id)).map(account => {
                const children = data.accounts.filter(child => child.credential_account_id === account.id);
                const catalog = data.services.find(item => item.id === account.service);
                return <section
                    key={account.id}
                    aria-label={`${account.name} account`}
                    className={`${card} overflow-hidden`}
                >
                    <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
                        <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="min-w-0 break-words text-[0.84375rem] font-medium text-ink">
                                    {account.name}
                                </span>
                                {setupFlag(account)}
                            </div>
                            <span className="mt-0.5 block break-words text-[0.71875rem] font-mono text-ink-2">
                                {catalog ? serviceLabel(catalog) : account.service}
                                {account.kind === 'custom'
                                    ? ` · ${String(account.metadata.custom_type ?? 'custom')}`
                                    : ` · ${authLabel(account.auth_type, catalog)}`}
                                {account.kind !== 'custom' && ['cli', 'subscription'].includes(account.auth_type) && account.metadata.harness
                                    ? ` · ${harnessLabel(String(account.metadata.harness))}` : ''}
                                {account.model ? ` · ${account.model}` : ''}
                            </span>
                        </div>
                        <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
                            {editControl(account)}
                            {trash(account)}
                        </div>
                    </div>
                    {confirmDelete === account.id && deleteConfirm(account, 'Runners using it will need another account.')}
                    {(children.length > 0 || supportsModels(account)) && <div className="flex flex-col gap-1.5 border-t border-line bg-sheet-2 px-3 py-2.5">
                        <span className={sectionLabel}>Models</span>
                        {children.map(child => {
                            const childError = child.model_configuration_error;
                            return <div key={child.id} className={card}>
                                <div className="flex flex-wrap items-center gap-2 px-2.5 py-2">
                                    <div className="min-w-0 flex-1">
                                        <div className="flex flex-wrap items-center gap-2">
                                            <span className="min-w-0 break-words text-[0.8125rem] text-ink">
                                                {child.name}
                                            </span>
                                            {setupFlag(child)}
                                        </div>
                                        <span className="mt-0.5 block break-words text-[0.71875rem] font-mono text-ink-2">
                                            {[child.metadata.harness, child.model].filter(Boolean).join(' · ')}
                                        </span>
                                    </div>
                                    <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
                                        {childError && <span
                                            className={cn(chip, 'flex-none border-red/40 bg-red-soft font-mono uppercase tracking-wider text-red')}
                                        >Incompatible</span>}
                                        {editControl(child)}
                                        {trash(child)}
                                    </div>
                                </div>
                                {childError && <p className="px-2.5 pb-2 text-[0.71875rem] text-red">
                                    {childError}
                                </p>}
                                {confirmDelete === child.id && deleteConfirm(child, 'Runners using it will need another entry.')}
                            </div>;
                        })}
                        {supportsModels(account) && <button className={`${btn} self-start`} onClick={() => addChild(account)}>Add model</button>}
                    </div>}
                </section>;
            })}
            {data?.accounts.length === 0 && <div
                className="rounded-md border border-dashed border-line-2 p-6 text-center text-[0.75rem] text-ink-3"
            >No accounts yet.</div>}
        </>}

        {view === 'add' && <section className={`${card} flex flex-col gap-3 p-3`} aria-label="New account">
            <div className="flex flex-col gap-1.5">
                <span className={formLabel}>Account type</span>
                <div className="flex flex-wrap gap-1" role="group" aria-label="Account type">
                    {(['supported', 'custom'] as const).map(option => <button
                        key={option}
                        type="button"
                        aria-pressed={addType === option}
                        className={`grow whitespace-nowrap ${addType === option ? segmentOn : segmentOff}`}
                        onClick={() => {
                            setAddType(option);
                            if (option === 'custom') setAddService('');
                        }}
                    >
                        {option === 'supported' ? 'Supported service' : 'Custom'}
                    </button>)}
                </div>
            </div>
            {addType === 'supported' && <label className="flex flex-col gap-1">
                <span className={formLabel}>Service</span>
                <select
                    className={formSelect}
                    aria-label="Service"
                    value={addService}
                    onChange={e => setAddService(e.target.value)}
                >
                    <option value="">Choose a service…</option>
                    {byLabel((data?.services ?? []).map(item => ({ value: item.id, label: serviceLabel(item) }))).map(option => <option key={option.value} value={option.value}>
                        {option.label}
                    </option>)}
                </select>
            </label>}
            <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-3">
                <button className={btn} onClick={() => setView('list')}>Cancel</button>
                <button
                    className={btnPrimary}
                    disabled={!addType || (addType === 'supported' && !addService)}
                    onClick={startDetail}
                >Next</button>
            </div>
        </section>}

        {view === 'detail' && editing && <fieldset className="flex flex-col gap-3" disabled={busy}>
            <div
                className={`${card} break-words px-3 py-2 text-[0.6875rem] font-mono uppercase tracking-wider text-ink-2`}
            >
                {strip}
            </div>
            {isChild && <label className="flex flex-col gap-1">
                <span className={formLabel}>Name</span>
                <input
                    aria-label="Name"
                    className={formInput}
                    value={editing.name}
                    onChange={e => patch({ name: e.target.value })}
                />
            </label>}

            {editing.kind === 'custom' && <label className="flex flex-col gap-1">
                <span className={formLabel}>Custom type</span>
                <input
                    aria-label="Custom type"
                    className={formInput}
                    placeholder="For example: deployment, database, issue_tracker"
                    value={String(editing.metadata.custom_type ?? '')}
                    onChange={e => patch({ metadata: { ...editing.metadata, custom_type: e.target.value } })}
                />
                <span className="text-[0.6875rem] text-ink-3">Generators use this type to request compatible entries.</span>
            </label>}

            {!isChild && editing.kind !== 'custom' && <>
                <label className="flex flex-col gap-1">
                    <span className={formLabel}>Authentication</span>
                    <select
                        className={formSelect}
                        aria-label="Authentication"
                        value={editing.auth_type}
                        onChange={e => selectAuthentication(e.target.value)}
                    >
                        {byLabel(Array.from(new Set([editing.auth_type, ...authTypes])).map(value => ({ value, label: authLabel(value, service) }))).map(option => <option key={option.value} value={option.value}>
                            {option.label}
                        </option>)}
                    </select>
                </label>
                {authHelp && <p className="-mt-1 text-[0.6875rem] text-ink-3">
                    {authHelp}
                </p>}
                {endpointInBody && <label className="flex flex-col gap-1">
                    <span className={formLabel}>{gitAddress?.label ?? 'Server URL'}</span>
                    <input
                        aria-label={gitAddress?.label ?? 'Server URL'}
                        className={formInput}
                        placeholder={gitAddress?.placeholder ?? 'https://your-model-endpoint/v1'}
                        value={editing.endpoint}
                        onChange={e => patch({ endpoint: e.target.value })}
                    />
                    <span className="text-[0.6875rem] text-ink-3">{gitAddress ? gitAddress.hint : `This endpoint must speak the ${editing.protocol} protocol.`}</span>
                </label>}
                {editing.auth_type === 'basic' && <label className="flex flex-col gap-1">
                    <span className={formLabel}>Username</span>
                    <input
                        aria-label="Username"
                        className={formInput}
                        value={String(editing.metadata.username ?? '')}
                        onChange={e => patch({ metadata: { ...editing.metadata, username: e.target.value } })}
                    />
                </label>}
                {editing.auth_type === 'api_key' && secretInput(authLabel('api_key', service), 'api_key')}
                {editing.auth_type === 'basic' && secretInput(editing.kind === 'git' ? 'Token or password' : 'Password', 'password')}
                {editing.auth_type === 'ssh_key' && <>
                    {secretTextarea('SSH private key', 'private_key')}
                    <label className="flex flex-col gap-1">
                        <span className={formLabel}>
                            Known hosts{atDefaultHost && editing.service === 'github' ? ' (not needed for github.com)'
                                : atDefaultHost && editing.service === 'gitlab' ? ' (not needed for gitlab.com)' : ''}
                        </span>
                        <textarea
                            aria-label="Known hosts"
                            className={cn(formTextarea, 'font-mono')}
                            rows={3}
                            spellCheck={false}
                            value={String(editing.metadata.known_hosts ?? '')}
                            onChange={e => patch({ metadata: { ...editing.metadata, known_hosts: e.target.value } })}
                        />
                        <span className="text-[0.6875rem] text-ink-3">One or more lines from ssh-keyscan -t ed25519 HOST; runs trust only these keys and never a host on first use.</span>
                    </label>
                </>}
                {editing.auth_type === 'subscription' && secretInput('Access token', 'access_token')}
                {editing.auth_type === 'cli' && <>
                    <label className="flex flex-col gap-1">
                        <span className={formLabel}>Login app</span>
                        {/* A pasted document belongs to the app it came from, so changing the app drops it. */}
                        <select
                            className={formSelect}
                            aria-label="Login app"
                            value={String(editing.metadata.harness ?? '')}
                            onChange={e => {
                                patch({ metadata: { ...editing.metadata, harness: e.target.value } });
                                setSecrets({});
                            }}
                        >
                            <option value="">Choose a login app…</option>
                            {Object.keys(planLogins).sort().map(app => <option key={app}>
                                {app}
                            </option>)}
                        </select>
                    </label>
                    <label className="flex flex-col gap-1">
                        <span className={formLabel}>Subscription</span>
                        <select
                            className={formSelect}
                            aria-label="Subscription"
                            value={String(editing.metadata.plan ?? '')}
                            onChange={e => patch({ metadata: { ...editing.metadata, plan: e.target.value } })}
                        >
                            <option value="">Choose a subscription…</option>
                            {plans.map(plan => <option key={plan}>
                                {plan}
                            </option>)}
                        </select>
                    </label>
                    <label className="flex flex-col gap-1">
                        <span className={formLabel}>{loginWhat === 'token' ? 'Login token' : 'Login document'}{planLogin?.optional ? ' (optional)' : ''}</span>
                        <textarea
                            aria-label={loginWhat === 'token' ? 'Login token' : 'Login document'}
                            className={formTextarea}
                            rows={loginWhat === 'token' ? 2 : 4}
                            disabled={!planLogin}
                            placeholder={!planLogin ? 'Choose a login app first'
                                : existing ? `Leave blank to keep the saved ${loginWhat}`
                                : planLogin.optional ? `Optional: paste the contents of ${basename(planLogin.store)} for the Annotation and Chat surfaces`
                                : planLogin.store ? `Paste the contents of ${basename(planLogin.store)} here` : 'Paste the token here'}
                            value={(planLogin && secrets[planLogin.field]) || ''}
                            onChange={e => {
                                if (!planLogin) return;
                                setSecrets({ ...secrets, [planLogin.field]: e.target.value });
                                setCheck(null);
                            }}
                        />
                        {planLogin && <span className="text-[0.6875rem] text-ink-3">
                            {planLogin.hint}
                        </span>}
                    </label>
                </>}
                <p className="text-[0.6875rem] text-ink-3">Secrets are stored in the server’s system keyring.</p>
            </>}

            {editing.kind === 'git' && <section className="flex flex-col gap-2.5" aria-label="Commit author">
                <label className="flex flex-col gap-1">
                    <span className={formLabel}>Commit author name</span>
                    <input
                        aria-label="Commit author name"
                        className={formInput}
                        autoComplete="name"
                        value={authorName}
                        onChange={e => patch({ metadata: { ...editing.metadata, author_name: e.target.value } })}
                    />
                    {!authorName.trim() && <span className="text-[0.6875rem] text-red">Required</span>}
                </label>
                <label className="flex flex-col gap-1">
                    <span className={formLabel}>Commit author email</span>
                    <input
                        aria-label="Commit author email"
                        className={formInput}
                        type="email"
                        autoComplete="email"
                        value={authorEmail}
                        onChange={e => patch({ metadata: { ...editing.metadata, author_email: e.target.value } })}
                    />
                    {!EMAIL.test(authorEmail) && <span className="text-[0.6875rem] text-red">Required</span>}
                </label>
                <p className="text-[0.6875rem] text-ink-3">Pushes and every commit the app makes for you with this account (Launch and agent git included) use this name and email. It is not a signature.</p>
            </section>}

            {editing.kind === 'custom' && <section className={`${card} flex flex-col gap-2.5 p-3`}>
                <div>
                    <span className={sectionLabel}>Custom fields</span>
                    <p className="mt-1 text-[0.6875rem] text-ink-3">Field names start with a letter and contain letters, numbers or underscores. Mark private values as secrets to store them in the system keyring.</p>
                </div>
                {fields.map((field, index) => <fieldset key={index} className={`${card} flex flex-col gap-2 p-2.5`}>
                    <legend className="px-1 text-[0.6875rem] font-mono text-ink-3">Field {index + 1}
                    </legend>
                    <div className="flex flex-wrap items-end gap-2">
                        <label className="flex min-w-0 flex-1 basis-40 flex-col gap-1">
                            <span className={formLabel}>Field name {index + 1}
                            </span>
                            <input
                                aria-label={`Field name ${index + 1}`}
                                className={formInput}
                                value={field.name}
                                onChange={e => editFields(fields.map((item, row) => row === index ? { ...item, name: e.target.value } : item))}
                            />
                        </label>
                        <button
                            className={btn}
                            aria-label={`Remove field ${index + 1}`}
                            onClick={() => editFields(fields.filter((_, row) => row !== index))}
                        >Remove</button>
                    </div>
                    <label className="flex items-center gap-2 text-[0.75rem] text-ink-2">
                        <input
                            type="checkbox"
                            className="accent-teal"
                            checked={field.secret}
                            onChange={e => editFields(fields.map((item, row) => row === index ? { ...item, secret: e.target.checked, value: e.target.checked ? item.value : '' } : item))}
                        />Store field {index + 1} as a secret</label>
                    <label className="flex flex-col gap-1">
                        <span className={formLabel}>
                            {field.secret ? 'Secret' : 'Value'} for field {index + 1}
                        </span>
                        <input
                            aria-label={`${field.secret ? 'Secret' : 'Value'} for field ${index + 1}`}
                            className={formInput}
                            type={field.secret ? 'password' : 'text'}
                            autoComplete={field.secret ? 'new-password' : 'off'}
                            placeholder={field.secret && field.savedSecretName === field.name.trim() ? 'Leave blank to keep saved secret' : ''}
                            value={field.value}
                            onChange={e => editFields(fields.map((item, row) => row === index ? { ...item, value: e.target.value } : item))}
                        />
                    </label>
                </fieldset>)}
                <button
                    className={`${btn} self-start`}
                    onClick={() => editFields([...fields, { name: '', secret: false, value: '' }])}
                >Add field</button>
            </section>}

            {editing.kind === 'model' && <>
                {inheritsHarness ? <p className="text-[0.75rem] text-ink-2">
                    Uses {harnessLabel(selectedHarness)} from this account
                </p> : <label className="flex flex-col gap-1">
                    <span className={formLabel}>Harness</span>
                    <select
                        className={formSelect}
                        aria-label="Harness"
                        value={selectedHarness}
                        onChange={e => patch({ metadata: { ...editing.metadata, harness: e.target.value } })}
                    >
                        {byLabel(harnessOptions.map(value => ({ value, label: harnessLabel(value) }))).map(option => <option key={option.value} value={option.value}>
                            {option.label}
                        </option>)}
                        {invalidHarness && <option value={selectedHarness} disabled>
                            {harnessLabel(selectedHarness)} (unavailable)</option>}
                    </select>
                </label>}
                {(invalidHarness || (configurationError && !dirty)) && <p role="alert" className="text-[0.71875rem] text-red">
                    {configurationError || 'This harness cannot use the account’s provider, connection or authentication.'} {harnessOptions.length ? 'Choose an available harness.' : 'Update the parent account to use a supported connection.'}
                </p>}
                <div className="flex flex-col gap-1">
                    <label htmlFor="account-model" className={formLabel}>Model</label>
                    <input
                        id="account-model"
                        aria-label="Model"
                        className={formInput}
                        list="account-models"
                        placeholder={discovering ? 'Loading models…' : modelRequired ? 'Enter a model ID — this endpoint has no default model' : `Harness default — leave blank to let ${harnessLabel(selectedHarness)} choose`}
                        value={editing.model}
                        onChange={e => {
                            const model = models.find(item => item.id === e.target.value);
                            patch({
                                model: e.target.value,
                                capabilities: model?.capabilities?.length ? model.capabilities : modelDefaults(parent ?? editing),
                                ...(!editing.name || editing.name === editing.model ? { name: e.target.value } : {})
                            });
                        }}
                    />
                    <datalist id="account-models">
                        {models.map(model => <option key={model.id} value={model.id}>
                            {model.name}
                        </option>)}
                    </datalist>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <button
                            className={btn}
                            disabled={discovering || (!isChild && (!existing || dirty))}
                            onClick={() => void loadModels()}
                        >Get current models</button>
                        <span className="text-[0.6875rem] text-ink-3">
                            {discovering ? 'Retrieving the model list…' : models.length ? `${models.length} available models` : 'Manual model IDs are supported.'}
                        </span>
                    </div>
                </div>
            </>}

            {editing.kind !== 'custom' && <div className={`${card} flex flex-col gap-2 p-3`}>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    <button className={btn} onClick={() => void verify()}>Verify</button>
                    <span className="min-w-0 flex-1 basis-40 text-[0.71875rem]">
                        {check === null ? <span className="text-ink-3">Test these credentials without saving them.</span>
                            : check.valid ? <span role="status" className="text-green">Verified{check.message ? ` — ${check.message}` : ''}
                            </span>
                                : <span role="status" className={check.valid === null ? 'text-ink-2' : 'text-red'}>
                                    {check.message || 'The account could not be verified.'}
                                </span>}
                    </span>
                </div>
                {check?.public_key && <label className="flex flex-col gap-1">
                    <span className={formLabel}>Public key — add this to the server</span>
                    <textarea
                        aria-label="Public key — add this to the server"
                        className={cn(formTextarea, 'font-mono')}
                        rows={3}
                        readOnly
                        spellCheck={false}
                        value={check.public_key}
                    />
                </label>}
            </div>}

            {editing.kind !== 'custom' && editing.kind !== 'git' && <details className={`${card} p-3`}>
                <summary className="cursor-pointer text-[0.75rem] text-ink-2 hover:text-ink">Advanced settings</summary>
                <div className="mt-3 flex flex-col gap-2.5">
                    {!isChild && <>
                        <label className="flex flex-col gap-1">
                            <span className={formLabel}>Protocol</span>
                            <input
                                aria-label="Protocol"
                                className={formInput}
                                list="account-protocols"
                                value={editing.protocol}
                                onChange={e => patch({ protocol: e.target.value })}
                            />
                            <datalist id="account-protocols">
                                {(service?.protocols ?? MODEL_PROTOCOLS).map(protocol => <option key={protocol} value={protocol} />)}
                            </datalist>
                        </label>
                        {!endpointInBody && <label className="flex flex-col gap-1">
                            <span className={formLabel}>Server URL</span>
                            <input
                                aria-label="Server URL"
                                className={formInput}
                                placeholder="Service default"
                                value={editing.endpoint}
                                onChange={e => patch({ endpoint: e.target.value })}
                            />
                            <span className="text-[0.6875rem] text-ink-3">Filled from the service default. Change this for a compatible endpoint.</span>
                        </label>}
                    </>}
                    {isChild && <p className="break-all text-[0.6875rem] font-mono text-ink-3">
                        {parent?.protocol ?? editing.protocol} · {parent?.endpoint || editing.endpoint || 'Service default endpoint'}
                    </p>}
                    {editing.kind === 'model' && <label className="flex flex-col gap-1">
                        <span className={formLabel}>Capabilities</span>
                        <input
                            aria-label="Capabilities"
                            className={formInput}
                            placeholder="Comma-separated: text, reasoning, tools, vision"
                            value={editing.capabilities.join(', ')}
                            onChange={e => patch({ capabilities: e.target.value.split(',').map(value => value.trim()) })}
                        />
                        <span className="text-[0.6875rem] text-ink-3">Filled from the discovered model. Generators filter model entries by these capabilities.</span>
                    </label>}
                </div>
            </details>}
            {!isChild && <label className="flex flex-col gap-1">
                <span className={formLabel}>Account label</span>
                <input
                    aria-label="Account label"
                    className={formInput}
                    value={editing.name}
                    onChange={e => patch({ name: e.target.value })}
                />
                <span className="text-[0.6875rem] text-ink-3">How this account is listed here and in runner dropdowns.</span>
            </label>}

            <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-3">
                <button className={btn} onClick={toList}>Cancel</button>
                <button
                    className={btnPrimary}
                    disabled={invalidHarness || !editing.name.trim() || !editing.service || (editing.kind === 'custom' && !String(editing.metadata.custom_type ?? '').trim()) || (editing.kind === 'model' && modelRequired && !editing.model.trim()) || gitIncomplete}
                    onClick={() => void save()}
                >Save</button>
            </div>
        </fieldset>}
    </div>;
}

/** The Key manager as a dialog (from a runner editor, the channel-creation
 *  view): above the full-window view and the Commands pane over it. */
export function KeyManagerOverlay({ onClose }: { onClose: () => void }) {
    useGuestOccluder('all', true);
    const overlay = useRef<HTMLDivElement>(null);
    useModalFocus(overlay, true, 'button');
    return createPortal(<div
        ref={overlay}
        {...KEYBOARD_OWNER_PROPS}
        data-pick-surface=""
        className="fixed inset-0 z-[460] flex items-center justify-center bg-(--lift) p-4"
        role="dialog"
        aria-modal="true"
        aria-label="Key manager"
        onKeyDown={e => trapModalKeys(e, overlay.current, onClose)}
    >
        <div
            className={cn(dialog, 'flex max-h-[90vh] w-full max-w-md flex-col')}
        >
            <div className="flex flex-none items-center justify-between gap-3 border-b border-line px-5 py-3">
                <div>
                    <h2 className="text-[0.90625rem] font-semibold text-ink">Key manager</h2>
                    <p className="mt-0.5 text-[0.75rem] text-ink-2">Secrets are stored in the server’s system keyring, separate from channel files.</p>
                </div>
                <button
                    className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem]')}
                    aria-label="Close Key manager"
                    title="Close Key manager"
                    onClick={onClose}
                >
                    <X className="h-4 w-4" />
                </button>
            </div>
            <div className="overflow-y-auto p-5">
                <KeyManager />
            </div>
        </div>
    </div>, document.body);
}
