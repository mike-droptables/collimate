import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { KeyManager, KeyManagerOverlay } from './KeyManager';
import { checkAccountDraft, fetchAccounts, type Account, type AccountService, type AccountsPayload } from '@/lib/accountsApi';
import { requestJson } from '@/lib/api/client';
import { retiredOr } from '@/test/retiredTokens';
import { expectButtonRowsWrap, expectFitsPaneColumn, expectStackedFields, renderInPaneColumn } from './paneColumn.testkit';
vi.mock('@/lib/accountsApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchAccounts: vi.fn(), checkAccountDraft: vi.fn(async () => ({ valid: true })) }));

vi.mock('@/lib/api/client', () => ({ requestJson: vi.fn(async () => ({})) }));

/** The catalog carries one instruction sentence per service and method — what
 *  to paste and where it comes from. Every method a service offers has one. */
const help: Record<string, Record<string, string>> = {
    openai: {
        api_key: 'Create an API key at https://platform.openai.com/api-keys and paste it here.',
        cli: 'Sign in with the app in a terminal on this machine, then paste the file it writes.',
    },
    anthropic: {
        api_key: 'Create an API key in the Anthropic console and paste it here.',
        subscription: 'Run claude setup-token in a terminal on this machine and paste the token it prints.',
    },
    github: {
        api_key: 'Create a personal access token on github.com and paste it here.',
        ssh_key: 'Paste an OpenSSH private key without a passphrase.',
    },
    gitlab: {
        api_key: 'Create a personal access token on your GitLab and paste it here.',
        ssh_key: 'Paste an OpenSSH private key without a passphrase.',
    },
    git: {
        ssh_key: 'Paste a private key the server accepts and its known_hosts lines.',
        basic: 'Enter the server address, a user name and a token.',
        none: 'Public repositories over HTTPS.',
    },
    custom: {
        api_key: 'Paste the API key your own endpoint expects.',
        basic: 'Paste the username and password your own endpoint expects.',
        none: 'This endpoint takes no credentials.',
    },
};
const services: AccountService[] = [
    { id: 'openai', name: 'OpenAI', protocols: ['openai'], auth_types: ['api_key', 'cli'], kinds: ['account', 'model'], endpoint: 'https://api.openai.com/v1', capabilities: ['text'], help: help.openai },
    { id: 'anthropic', name: 'Anthropic', protocols: ['anthropic'], auth_types: ['api_key', 'subscription'], kinds: ['account', 'model'], endpoint: 'https://api.anthropic.com', capabilities: ['text'], help: help.anthropic },
    { id: 'github', name: 'GitHub', protocols: ['github'], auth_types: ['api_key', 'ssh_key'], kinds: ['git'], auth_labels: { api_key: 'Personal access token', ssh_key: 'SSH key' }, endpoint: 'https://github.com', capabilities: ['git'], help: help.github },
    { id: 'gitlab', name: 'GitLab', protocols: ['gitlab'], auth_types: ['api_key', 'ssh_key'], kinds: ['git'], auth_labels: { api_key: 'Personal access token', ssh_key: 'SSH key' }, endpoint: 'https://gitlab.com', capabilities: ['git'], help: help.gitlab },
    { id: 'git', name: 'Git server', protocols: ['git'], auth_types: ['ssh_key', 'basic', 'none'], kinds: ['git'], auth_labels: { basic: 'Username and token', ssh_key: 'SSH key', none: 'Public (no credentials)' }, endpoint: '', capabilities: ['git'], help: help.git },
    { id: 'custom', name: 'Custom service', protocols: ['openai-compatible'], auth_types: ['api_key', 'basic', 'none'], kinds: ['custom', 'account', 'model'], endpoint: '', capabilities: [], help: help.custom },
];
const response: AccountsPayload = {
    accounts: [], services, status: { available: true, config_path: '/config/accounts.json' },
    model_harnesses: ['pydantic-ai', 'claude-code', 'antigravity', 'codex', 'goose', 'opencode', 'qwen-code', 'cline', 'pi'],
    default_model_harness: 'pydantic-ai',
    harness_labels: { 'pydantic-ai': 'Direct model API' },
    login_plans: {
        openai: { plan: 'chatgpt', harness: 'codex' }, anthropic: { plan: 'claude_code', harness: 'claude-code' },
        gemini: { plan: 'google', harness: 'antigravity' }, copilot: { plan: 'github_copilot', harness: 'copilot' },
        xai: { plan: 'xai', harness: 'opencode' },
    },
    plan_logins: {
        codex: { command: 'codex login', store: '/home/me/.codex/auth.json', field: 'credential_json', hint: 'Sign in with `codex login`, then paste the contents of /home/me/.codex/auth.json.', optional: false },
        opencode: { command: 'opencode auth login', store: '/home/me/.local/share/opencode/auth.json', field: 'credential_json', hint: 'The contents of /home/me/.local/share/opencode/auth.json.', optional: false },
        antigravity: { command: 'the antigravity-acp server (in Zed or JetBrains)', store: '/home/me/.gemini/antigravity-acp/acp_token.json', field: 'credential_json', hint: 'Optional, for the Annotation and Chat surfaces: the whole contents of /home/me/.gemini/antigravity-acp/acp_token.json. At your own risk: a Gemini API key is the supported route. The CLI surface needs no document: it runs agy on this machine, which signs in through Google\'s own browser flow.', optional: true },
        copilot: { command: 'a fine-grained personal access token', store: '', field: 'credential_file', hint: 'Create a fine-grained personal access token with the "Copilot Requests" permission and paste it here.', optional: false },
    },
};
const AUTHOR = { author_name: 'Octo', author_email: 'octo@example.com' };
const account = (over: Partial<Account>): Account => ({ id: 'primary', name: 'Primary', kind: 'account', service: 'openai', protocol: 'openai', endpoint: 'https://api.openai.com/v1', model: '', capabilities: ['text'], auth_type: 'api_key', metadata: {}, configured: true, generation: 'same-generation', ...over });
const optionText = (label: string) => within(screen.getByLabelText(label) as HTMLElement).getAllByRole('option').map(option => option.textContent);
const body = (path: string, method: string) => vi.mocked(requestJson).mock.calls.find(call => call[0] === path && call[1] === method)?.[2] as Record<string, unknown>;
const addSupported = (service: string) => {
    fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
    fireEvent.click(screen.getByRole('button', { name: 'Supported service' }));
    fireEvent.change(screen.getByLabelText('Service'), { target: { value: service } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
};
/** A new, never-saved OpenAI account on the app-login method. */
const startPlanDraft = () => {
    addSupported('openai');
    fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'ChatGPT' } });
    fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'cli' } });
};
beforeEach(() => { vi.resetAllMocks(); vi.mocked(requestJson).mockResolvedValue({}); vi.mocked(checkAccountDraft).mockResolvedValue({ valid: true }); vi.mocked(fetchAccounts).mockResolvedValue(response); });
afterEach(cleanup);

describe('Global Key manager', () => {
    it('does not replace credentials changed by another editor (F0395)', async () => {
        const old = account({ revision: 'old' });
        const current = { ...old, revision: 'new', generation: 'other-generation' };
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [current] }).mockResolvedValueOnce({ ...response, accounts: [old] });
        vi.mocked(requestJson).mockRejectedValueOnce(Object.assign(new Error('Account changed'), { status: 409 })).mockResolvedValue(current);
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'my-new-key' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await screen.findByText(/Credentials changed in another editor/);
        expect(requestJson).toHaveBeenCalledTimes(1);
        expect(screen.getByLabelText('API key')).toHaveValue('my-new-key');
    });
    it('rebases account edits once after a background refresh conflict (F0395)', async () => {
        const old = account({ service: 'anthropic', protocol: 'anthropic', revision: 'old', metadata: { region: 'eu' } });
        const current = { ...old, revision: 'refreshed', metadata: { ...old.metadata, refreshed_cache: 'kept' } };
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [current] }).mockResolvedValueOnce({ ...response, accounts: [old] });
        vi.mocked(requestJson).mockRejectedValueOnce(Object.assign(new Error('Account changed'), { status: 409 })).mockResolvedValue({ ...current, name: 'My Anthropic' });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'My Anthropic' } });
        fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'typed-key' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledTimes(2));
        expect(vi.mocked(requestJson).mock.calls[1]).toEqual(['/accounts/primary', 'PUT', expect.objectContaining({
            name: 'My Anthropic', revision: 'refreshed', metadata: { region: 'eu', refreshed_cache: 'kept' }, secrets: { api_key: 'typed-key' },
        })]);
        await waitFor(() => expect(screen.getByRole('button', { name: 'Add account' })).toBeTruthy());
    });
    it('keeps the draft after a second conflict without another retry (F0395)', async () => {
        const old = account({ revision: 'old' });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [{ ...old, revision: 'new' }] }).mockResolvedValueOnce({ ...response, accounts: [old] });
        vi.mocked(requestJson).mockRejectedValue(Object.assign(new Error('Still changed'), { status: 409 }));
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Edited' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await screen.findByText('Still changed');
        expect(requestJson).toHaveBeenCalledTimes(2);
        expect(screen.getByLabelText('Account label')).toHaveValue('Edited');
    });
    it('does not overwrite a concurrent edit of the same field (F0395)', async () => {
        const old = account({ revision: 'old' });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [{ ...old, revision: 'new', name: 'Their edit' }] }).mockResolvedValueOnce({ ...response, accounts: [old] });
        vi.mocked(requestJson).mockRejectedValue(Object.assign(new Error('Account changed'), { status: 409 }));
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'My edit' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await screen.findByText(/same field changed/);
        expect(requestJson).toHaveBeenCalledTimes(1);
        expect(screen.getByLabelText('Account label')).toHaveValue('My edit');
    });
    it('shows config parse diagnostics at the top without requiring a channel', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, status: { ...response.status, error: 'Invalid JSON at line 7' } });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Invalid JSON at line 7'));
        expect(screen.getByRole('alert')).toHaveTextContent('/config/accounts.json');
        expect(screen.getByRole('button', { name: 'Add account' })).toBeTruthy();
        expect(screen.getByText('No accounts yet.')).toBeTruthy();
    });
    it('fixes the account type and service before the detail form opens (F0953)', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
        expect(screen.queryByLabelText('Service')).toBeNull();
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.click(screen.getByRole('button', { name: 'Supported service' }));
        expect(optionText('Service')).toEqual(['Choose a service…', 'Anthropic', 'Custom model endpoint', 'Git server', 'GitHub', 'GitLab', 'OpenAI']);
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Service'), { target: { value: 'openai' } });
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByText('Supported service · OpenAI')).toBeTruthy();
        expect(screen.queryByLabelText('Account name')).toBeNull();
        expect(screen.getByLabelText('Account label')).toHaveValue('');
        expect(screen.queryByLabelText('Service')).toBeNull();
        expect(screen.queryByLabelText('Capabilities')).toBeNull();
    });
    it('sorts authentication by label and instructs from the catalog for the chosen method', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        expect(optionText('Authentication')).toEqual(['API key', 'App login']);
        expect(screen.getByLabelText('Authentication')).toHaveValue('api_key');
        expect(screen.getByText(help.openai.api_key)).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'cli' } });
        expect(optionText('Authentication')).toEqual(['API key', 'App login']);
        expect(screen.getByText(help.openai.cli)).toBeTruthy();
        expect(screen.queryByText(help.openai.api_key)).toBeNull();
    });
    it('instructs from the catalog for every method each service offers', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        for (const catalog of services) {
            addSupported(catalog.id);
            for (const auth of catalog.auth_types) {
                fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: auth } });
                expect(screen.getByLabelText('Authentication')).toHaveValue(auth);
                expect(screen.getByText(help[catalog.id][auth])).toBeTruthy();
            }
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        }
    });
    it('verifies the draft without saving it, and explains a failure', async () => {
        vi.mocked(checkAccountDraft).mockResolvedValue({ valid: false, message: 'Service returned HTTP 401' });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Thinking model' } });
        fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'test-secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(screen.getByText('Service returned HTTP 401')).toBeTruthy());
        expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({ name: 'Thinking model', kind: 'account', service: 'openai', auth_type: 'api_key', secrets: { api_key: 'test-secret' } }));
        expect(vi.mocked(checkAccountDraft).mock.calls[0][0]).not.toHaveProperty('id');
        // The instruction sits under Authentication, once; a failure carries only its message.
        expect(screen.getAllByText(help.openai.api_key)).toHaveLength(1);
        expect(requestJson).not.toHaveBeenCalled();
    });
    it('reports a successful verification', async () => {
        vi.mocked(checkAccountDraft).mockResolvedValue({ valid: true, message: '42 models available' });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(screen.getByText('Verified — 42 models available')).toBeTruthy());
    });
    it('saves an account without capabilities and returns to the list', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Thinking model' } });
        fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'test-secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({ name: 'Thinking model', kind: 'account', secrets: { api_key: 'test-secret' } })));
        expect(body('/accounts', 'POST')).not.toHaveProperty('capabilities');
        await waitFor(() => expect(screen.getByRole('button', { name: 'Add account' })).toBeTruthy());
        expect(screen.queryByLabelText('Account label')).toBeNull();
    });
    it('keeps the detail open when the save fails', async () => {
        vi.mocked(requestJson).mockRejectedValue(new Error('Account “thinking” already exists'));
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Thinking' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('already exists'));
        expect(screen.getByLabelText('Account label')).toHaveValue('Thinking');
    });
    it('keeps a successful save committed when the list refresh fails so a retry cannot duplicate it', async () => {
        vi.mocked(requestJson).mockResolvedValue(account({ id: 'thinking-1234', name: 'Thinking' }));
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Thinking' } });
        vi.mocked(fetchAccounts).mockRejectedValue(new Error('Service unavailable'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Service unavailable'));
        vi.mocked(fetchAccounts).mockResolvedValue(response);
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts/thinking-1234', 'PUT', expect.objectContaining({ name: 'Thinking' })));
        expect(vi.mocked(requestJson).mock.calls.filter(call => call[1] === 'POST')).toHaveLength(1);
    });
    it('drops a verification result as soon as any part of the draft changes', async () => {
        vi.mocked(checkAccountDraft).mockResolvedValue({ valid: true, message: '42 models available' });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(screen.getByText('Verified — 42 models available')).toBeTruthy());
        fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://typo.example/v1' } });
        expect(screen.queryByText(/Verified/)).toBeNull();
        expect(screen.getByText('Test these credentials without saving them.')).toBeTruthy();
    });
    it('offers a custom account neither Verify nor Advanced settings', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
        fireEvent.click(screen.getByRole('button', { name: 'Custom' }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByLabelText('Custom type')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Verify' })).toBeNull();
        expect(screen.queryByText('Advanced settings')).toBeNull();
        expect(checkAccountDraft).not.toHaveBeenCalled();
        // The label sits last, after the fields the account is made of.
        const label = screen.getByLabelText('Account label');
        expect(label.compareDocumentPosition(screen.getByLabelText('Custom type')) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    });
    it('pastes an app login document, and only once an app is chosen', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        expect(optionText('Login app')).toEqual(['Choose a login app…', 'antigravity', 'codex', 'copilot', 'opencode']);
        expect(screen.getByLabelText('Login app')).toHaveValue('codex');
        expect(screen.getByLabelText('Subscription')).toHaveValue('chatgpt');
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: '' } });
        expect(screen.getByLabelText('Login document')).toBeDisabled();
        expect(screen.getByLabelText('Login document')).toHaveAttribute('placeholder', 'Choose a login app first');
        expect(screen.queryByText(/contents of/)).toBeNull();
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: 'codex' } });
        expect(screen.getByLabelText('Login document')).toBeEnabled();
        expect(screen.getByLabelText('Login document')).toHaveAttribute('placeholder', 'Paste the contents of auth.json here');
        expect(screen.getByText(/paste the contents of \/home\/me\/\.codex\/auth\.json/)).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Login document'), { target: { value: '{"tokens":{"id":"x"}}' } });
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({
            name: 'ChatGPT', service: 'openai', auth_type: 'cli', secrets: { credential_json: '{"tokens":{"id":"x"}}' },
            metadata: expect.objectContaining({ harness: 'codex', plan: 'chatgpt' }),
        })));
    });
    it('keeps an app the user chose when the method is picked again', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: 'opencode' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'api_key' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'cli' } });
        expect(screen.getByLabelText('Login app')).toHaveValue('opencode');
        expect(screen.getByLabelText('Subscription')).toHaveValue('chatgpt');
    });
    it('saves an antigravity acp_token.json login, at the user\'s own risk, and drops a document the app changed away from', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        expect(screen.getByText('Login document')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Login document'), { target: { value: 'codex-document' } });
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: 'antigravity' } });
        expect(screen.getByLabelText('Login document')).toHaveValue('');
        // The document is optional: the CLI surface signs in through agy on this machine.
        expect(screen.getByText('Login document (optional)')).toBeTruthy();
        expect(screen.getByLabelText('Login document')).toHaveAttribute('placeholder', 'Optional: paste the contents of acp_token.json for the Annotation and Chat surfaces');
        expect(screen.getByText(/acp_token\.json\. At your own risk/)).toBeTruthy();
        expect(screen.getByText(/The CLI surface needs no document: it runs agy on this machine/)).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Login document'), { target: { value: 'antigravity-document' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            secrets: { credential_json: 'antigravity-document' }, metadata: expect.objectContaining({ harness: 'antigravity' }),
        })));
    });
    it('asks a Copilot login for a token, not a file\'s contents', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: 'copilot' } });
        expect(screen.queryByLabelText('Login document')).toBeNull();
        const token = screen.getByLabelText('Login token');
        expect(token).toHaveAttribute('placeholder', 'Paste the token here');
        expect(token.getAttribute('placeholder')).not.toMatch(/file contents/);
        expect(screen.getByText(/Copilot Requests/)).toBeTruthy();
        fireEvent.change(token, { target: { value: 'github_pat_x' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            secrets: { credential_file: 'github_pat_x' }, metadata: expect.objectContaining({ harness: 'copilot' }),
        })));
    });
    it('asks a Claude subscription for one pasted token and fixes the app it comes from', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('anthropic');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Claude' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'subscription' } });
        expect(screen.getByText(help.anthropic.subscription)).toBeTruthy();
        expect(screen.queryByLabelText('Login app')).toBeNull();
        expect(screen.queryByLabelText('Subscription')).toBeNull();
        expect(screen.queryByLabelText('Login document')).toBeNull();
        fireEvent.change(screen.getByLabelText('Access token'), { target: { value: 'pasted-token' } });
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({
            service: 'anthropic', auth_type: 'subscription', secrets: { access_token: 'pasted-token' },
            metadata: expect.objectContaining({ harness: 'claude-code', plan: 'claude_code' }),
        })));
    });
    it('offers no sign-in, pull or sign-out control on any authentication method', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        const gone = () => ['Sign in', 'Pull login', 'Sign out', 'Cancel sign-in'].forEach(name => expect(screen.queryByRole('button', { name })).toBeNull());
        startPlanDraft();
        gone();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        addSupported('anthropic');
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'subscription' } });
        gone();
    });
    it('hides the server connection behind advanced settings', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('openai');
        expect(screen.getByLabelText('Server URL')).toHaveValue('https://api.openai.com/v1');
        expect(screen.getByLabelText('Server URL').closest('details')).toBeTruthy();
        expect(screen.getByLabelText('Protocol').closest('details')).toBeTruthy();
    });
    it('asks for the server URL in the body when the service has no endpoint of its own', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('custom');
        const url = screen.getByLabelText('Server URL');
        expect(url.closest('details')).toBeNull();
        expect(url).toHaveAttribute('placeholder', 'https://your-model-endpoint/v1');
        expect(screen.getByText('This endpoint must speak the openai-compatible protocol.')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'My endpoint' } });
        fireEvent.change(url, { target: { value: 'https://box.local/v1' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({ endpoint: 'https://box.local/v1' })));
    });
    it('deletes an account from the list behind an inline confirmation', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({})] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Delete Primary' }));
        expect(screen.getByText(/Delete “Primary”\?/)).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
        expect(screen.queryByText(/Delete “Primary”\?/)).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Delete Primary' }));
        fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts/primary', 'DELETE'));
    });
    it('saves a Google plan entry with nothing pasted, its CLI signing in on this machine', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        fireEvent.change(screen.getByLabelText('Login app'), { target: { value: 'antigravity' } });
        fireEvent.change(screen.getByLabelText('Subscription'), { target: { value: 'google' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            metadata: expect.objectContaining({ harness: 'antigravity', plan: 'google' }),
        })));
        expect(body('/accounts', 'POST').secrets ?? {}).not.toHaveProperty('credential_json');
    });
    it.each([
        'docker: none uses this machine’s codex; docker: full uses its container; docker: limited cannot serve CLI harnesses',
        'needs docker: full, or install codex outside Snap for docker: none',
        'antigravity can run it: its CLI runs on this machine (agy 1.4.7 at /opt/bin/agy) under docker: none or full with commands ask or allow, outside any sandbox, signed in once for this entry',
        'nothing is pasted: the CLI surface runs Claude Code’s claude on this machine, which signs in itself the first time (one sign-in per Key manager entry); the Annotation and Chat surfaces need the pasted credential',
    ])('displays the API grant diagnostic unchanged (D06): %s', async message => {
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({}), model] });
        vi.mocked(checkAccountDraft).mockResolvedValue({ valid: true, message });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Fast' }));
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        expect(await screen.findByText(`Verified — ${message}`)).toBeTruthy();
        expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({ id: 'fast', metadata: { harness: 'codex' } }));
    });
    it('carries capabilities on a model entry and verifies it against its parent', async () => {
        const parent = account({});
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' }, capabilities: ['text', 'vision'] });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [parent, model] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: `Edit Fast` }));
        expect(screen.getByText('Model · Primary · OpenAI')).toBeTruthy();
        expect(screen.getByLabelText('Capabilities')).toHaveValue('text, vision');
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({ id: 'fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', capabilities: ['text', 'vision'] })));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts/fast', 'PUT', expect.objectContaining({ capabilities: ['text', 'vision'] })));
    });
    it('adds a model under an account and discovers its model list', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({})] });
        vi.mocked(requestJson).mockResolvedValue({ models: [{ id: 'gpt-x', name: 'GPT X', capabilities: ['text', 'vision'] }] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Add model' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts/primary/models', 'POST'));
        expect(screen.getByText('Model · Primary · OpenAI')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'gpt-x' } });
        expect(screen.getByLabelText('Name')).toHaveValue('gpt-x');
        expect(screen.getByLabelText('Capabilities')).toHaveValue('text, vision');
    });
    it('saves a model entry with no model, which runs the harness default', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({})] });
        vi.mocked(requestJson).mockResolvedValue({ models: [] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Add model' }));
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Harness default — leave blank to let Direct model API choose'));
        fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'codex' } });
        expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Harness default — leave blank to let codex choose');
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Codex default' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            kind: 'model', model: '', credential_account_id: 'primary', metadata: expect.objectContaining({ harness: 'codex' }),
        })));
    });
    // The API serves each account's default_model (null where the endpoint
    // has none); the form reads it rather than re-deriving the endpoint rule.
    it('asks for a model only where the served default says the endpoint has none', async () => {
        const box = account({ id: 'box', name: 'Box', service: 'custom', protocol: 'openai-compatible', endpoint: 'https://box.local/v1', default_model: null });
        const proxy = account({ id: 'proxy', name: 'Proxy', endpoint: 'https://proxy.local/v1', default_model: null });
        const slashed = account({ id: 'slashed', name: 'Slashed', endpoint: 'https://api.openai.com/v1/', default_model: 'gpt-5' });
        const tuned = account({ id: 'tuned', name: 'Tuned', service: 'custom', protocol: 'openai-compatible', endpoint: 'https://tuned.local/v1', default_model: 'tuned-default' });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [box, proxy, slashed, tuned] });
        vi.mocked(requestJson).mockResolvedValue({ models: [] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click((await screen.findAllByRole('button', { name: 'Add model' }))[0]);
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Enter a model ID — this endpoint has no default model'));
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Box model' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'box-model' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // A supported service pointed somewhere other than its catalog endpoint is served no default either.
        fireEvent.click(screen.getAllByRole('button', { name: 'Add model' })[1]);
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Enter a model ID — this endpoint has no default model'));
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Proxy model' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // An account served a default lets the model stay blank.
        fireEvent.click(screen.getAllByRole('button', { name: 'Add model' })[2]);
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Harness default — leave blank to let Direct model API choose'));
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Slashed model' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // The served value decides, whatever the endpoint looks like.
        fireEvent.click(screen.getAllByRole('button', { name: 'Add model' })[3]);
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Harness default — leave blank to let Direct model API choose'));
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Tuned model' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });
    it('saves a plan login model entry with no model', async () => {
        const plan = account({ id: 'chatgpt', name: 'ChatGPT', auth_type: 'cli', supported_harnesses: ['codex'], metadata: { harness: 'codex', plan: 'chatgpt' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [plan] });
        vi.mocked(requestJson).mockResolvedValue({ models: [] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Add model' }));
        await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAttribute('placeholder', 'Harness default — leave blank to let codex choose'));
        expect(screen.queryByLabelText('Harness')).toBeNull();
        expect(screen.getByText('Uses codex from this account')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Codex plan model' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            kind: 'model', model: '', auth_type: 'cli', credential_account_id: 'chatgpt', metadata: expect.objectContaining({ harness: 'codex' }),
        })));
    });
    // The harness and plan lists are the SDK taxonomy's, served with the
    // accounts; the Key manager hard-codes none of them.
    it('offers the harnesses the API serves', async () => {
        const parent = account({});
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'future-harness' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [parent, model], model_harnesses: ['pydantic-ai', 'future-harness'] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Fast' }));
        expect(optionText('Harness')).toEqual(['Direct model API', 'future-harness']);
    });
    it('starts a plan login on the app and plans the API serves', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response,
            login_plans: { openai: { plan: 'chatgpt', harness: 'opencode' }, xai: { plan: 'future_plan', harness: 'opencode' } } });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        startPlanDraft();
        expect(screen.getByLabelText('Login app')).toHaveValue('opencode');
        expect(optionText('Subscription')).toEqual(['Choose a subscription…', 'chatgpt', 'future_plan']);
    });
    it('orders the harness dropdown by its visible label', async () => {
        const parent = account({ supported_harnesses: ['pydantic-ai', 'codex', 'claude-code'] });
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [parent, model] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: `Edit Fast` }));
        expect(optionText('Harness')).toEqual(['claude-code', 'codex', 'Direct model API']);
    });
    it('marks an incompatible saved harness unavailable until a valid one is chosen', async () => {
        const parent = account({ supported_harnesses: ['pydantic-ai'] });
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' }, model_configuration_error: 'codex cannot use an API key account' });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [parent, model] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: `Edit Fast` }));
        expect(screen.getByRole('alert')).toHaveTextContent('codex cannot use an API key account');
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'pydantic-ai' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });
    it('offers GitHub a personal access token or an SSH key', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({ id: 'gh', name: 'GitHub', kind: 'git', service: 'github', protocol: 'github', endpoint: 'https://github.com', capabilities: ['git'], metadata: AUTHOR })] });
        renderInPaneColumn(<KeyManager />);
        const section = await screen.findByRole('region', { name: 'GitHub account' });
        expect(within(section).getByText('GitHub · Personal access token')).toBeInTheDocument();
        expect(within(section).queryByText('Models')).toBeNull();
        fireEvent.click(within(section).getByRole('button', { name: 'Edit GitHub' }));
        expect(optionText('Authentication')).toEqual(['Personal access token', 'SSH key']);
        expect(screen.getByText(help.github.api_key)).toBeInTheDocument();
        expect(screen.getByLabelText('Personal access token')).toHaveAttribute('type', 'password');
        expect(screen.queryByLabelText('API key')).toBeNull();
        expect(screen.queryByLabelText('Username')).toBeNull();
        expect(screen.getByLabelText('Server URL')).toHaveValue('https://github.com');
        expect(screen.getByLabelText('Account label')).toHaveValue('GitHub');
        expect(screen.getByRole('button', { name: 'Verify' })).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        expect(screen.getByText(help.github.ssh_key)).toBeInTheDocument();
        expect(screen.queryByLabelText('Personal access token')).toBeNull();
        expect(screen.getByLabelText('SSH private key').tagName).toBe('TEXTAREA');
    });
    it('starts a new GitHub entry as kind git on the personal access token', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('github');
        expect(screen.getByText('Supported service · GitHub')).toBeInTheDocument();
        expect(screen.getByLabelText('Authentication')).toHaveValue('api_key');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Mine' } });
        fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_x' } });
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Octo' } });
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'octo@example.com' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            kind: 'git', service: 'github', protocol: 'github', endpoint: 'https://github.com', auth_type: 'api_key',
            credential_account_id: null, secrets: { api_key: 'ghp_x' }, metadata: AUTHOR,
        })));
        expect(body('/accounts', 'POST')).not.toHaveProperty('capabilities');
    });
    it('asks for the commit author and keeps Save disabled until both are valid', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('gitlab');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Team GitLab' } });
        const author = screen.getByRole('region', { name: 'Commit author' });
        expect(within(author).getAllByText('Required')).toHaveLength(2);
        expect(within(author).getByText(/It is not a signature\./)).toBeInTheDocument();
        const save = screen.getByRole('button', { name: 'Save' });
        expect(save).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Ada Lovelace' } });
        expect(save).toBeDisabled();
        for (const email of ['ada', 'ada@', 'a da@example.org', '<ada@example.org>']) {
            fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: email } });
            expect(save).toBeDisabled();
            expect(within(author).getByText('Required')).toBeInTheDocument();
        }
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'ada@example.org' } });
        expect(within(author).queryByText('Required')).toBeNull();
        expect(save).toBeEnabled();
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: '   ' } });
        expect(save).toBeDisabled();
    });
    it('an SSH key method shows a write-only private key and Known hosts', async () => {
        const saved = account({ id: 'gh', name: 'GitHub', kind: 'git', service: 'github', protocol: 'github', endpoint: 'https://github.com', capabilities: ['git'], auth_type: 'ssh_key', secret_fields: ['private_key'], metadata: AUTHOR });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [saved] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit GitHub' }));
        const key = screen.getByLabelText('SSH private key');
        expect(key).toHaveValue('');
        expect(key).toHaveAttribute('placeholder', 'Leave blank to keep the saved key');
        expect(key).toHaveAttribute('spellcheck', 'false');
        expect(key).toHaveAttribute('autocomplete', 'off');
        expect(key).toHaveClass('font-mono');
        expect(screen.getByText('Known hosts (not needed for github.com)')).toBeInTheDocument();
        expect(screen.getByText(/never a host on first use/)).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://ghe.example.com' } });
        expect(screen.getByText('Known hosts')).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('Known hosts'), { target: { value: 'ghe.example.com ssh-ed25519 AAAA' } });
        cleanup();
        vi.mocked(fetchAccounts).mockResolvedValue(response);
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('git');
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        expect(screen.getByLabelText('SSH private key')).toHaveAttribute('placeholder', '-----BEGIN OPENSSH PRIVATE KEY-----');
        expect(screen.getByText('Known hosts')).toBeInTheDocument();
    });
    it('the GitLab URL sits in the body and the Git server\'s Server URL only for basic', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('gitlab');
        expect(screen.getByLabelText('GitLab URL')).toHaveValue('https://gitlab.com');
        expect(screen.getByText('https://gitlab.com, or your instance\'s address with its path if it has one')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        addSupported('git');
        expect(screen.queryByLabelText('Server URL')).toBeNull();
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'basic' } });
        expect(screen.getByLabelText('Server URL')).toHaveValue('');
        expect(screen.getByText('The username and token are sent only to repositories under this address')).toBeInTheDocument();
        expect(screen.getByLabelText('Username')).toBeInTheDocument();
        expect(screen.getByLabelText('Token or password')).toHaveAttribute('type', 'password');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Gitea' } });
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Ada' } });
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'ada@example.org' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://git.example.org' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'none' } });
        expect(screen.queryByLabelText('Server URL')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(body('/accounts', 'POST')).toMatchObject({ kind: 'git', service: 'git', auth_type: 'none', endpoint: '' }));
    });
    it('kind git shows no Advanced settings', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({ id: 'gl', name: 'GitLab', kind: 'git', service: 'gitlab', protocol: 'gitlab', endpoint: 'https://gitlab.com', capabilities: ['git'], metadata: AUTHOR })] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: 'Edit GitLab' }));
        expect(screen.queryByText('Advanced settings')).toBeNull();
        expect(screen.queryByLabelText('Protocol')).toBeNull();
        expect(screen.getAllByLabelText('GitLab URL')).toHaveLength(1);
    });
    it('switching authentication clears typed secrets', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('github');
        fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_typed' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        fireEvent.change(screen.getByLabelText('SSH private key'), { target: { value: '-----BEGIN OPENSSH PRIVATE KEY-----' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'api_key' } });
        expect(screen.getByLabelText('Personal access token')).toHaveValue('');
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        expect(screen.getByLabelText('SSH private key')).toHaveValue('');
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        await waitFor(() => expect(checkAccountDraft).toHaveBeenCalledWith(expect.objectContaining({ auth_type: 'ssh_key', secrets: {} })));
    });
    it('switching authentication drops the hidden username and known hosts of a Git entry', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('github');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'GitHub' } });
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Ada' } });
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'ada@example.org' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        fireEvent.change(screen.getByLabelText('Known hosts'), { target: { value: '|1|hashed' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'api_key' } });
        fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_typed' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(body('/accounts', 'POST')).toBeTruthy());
        expect(body('/accounts', 'POST').metadata).toEqual({ author_name: 'Ada', author_email: 'ada@example.org' });
        cleanup();
        vi.mocked(fetchAccounts).mockResolvedValue(response);
        vi.mocked(requestJson).mockClear();
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('git');
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Box' } });
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Ada' } });
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'ada@example.org' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'basic' } });
        fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'a:b' } });
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'none' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(body('/accounts', 'POST')).toBeTruthy());
        expect(body('/accounts', 'POST').metadata).toEqual({ author_name: 'Ada', author_email: 'ada@example.org' });
    });
    it('Verify shows the public key box', async () => {
        vi.mocked(checkAccountDraft).mockResolvedValue({ valid: true, message: 'Key accepted (ED25519 SHA256:abc); the host is not contacted', public_key: 'ssh-ed25519 AAAAC3Nza' });
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('gitlab');
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        expect(screen.queryByLabelText('Public key — add this to the server')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        const box = await screen.findByLabelText('Public key — add this to the server');
        expect(box).toHaveValue('ssh-ed25519 AAAAC3Nza');
        expect(box).toHaveAttribute('readonly');
        expect(screen.getByText(/Key accepted \(ED25519 SHA256:abc\)/)).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('Known hosts'), { target: { value: 'x' } });
        expect(screen.queryByLabelText('Public key — add this to the server')).toBeNull();
    });
    it('the POST body carries metadata.author_* and private_key only under secrets', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        addSupported('git');
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'ssh_key' } });
        const key = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA==\n-----END OPENSSH PRIVATE KEY-----\n';
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Box' } });
        fireEvent.change(screen.getByLabelText('SSH private key'), { target: { value: key } });
        fireEvent.change(screen.getByLabelText('Known hosts'), { target: { value: '[box.example.org]:2222 ssh-ed25519 AAAAC3' } });
        fireEvent.change(screen.getByLabelText('Commit author name'), { target: { value: 'Ada Lovelace' } });
        fireEvent.change(screen.getByLabelText('Commit author email'), { target: { value: 'ada@example.org' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(body('/accounts', 'POST')).toBeTruthy());
        const sent = body('/accounts', 'POST');
        expect(sent).toMatchObject({
            kind: 'git', service: 'git', protocol: 'git', endpoint: '', auth_type: 'ssh_key', secrets: { private_key: key },
            metadata: { author_name: 'Ada Lovelace', author_email: 'ada@example.org', known_hosts: '[box.example.org]:2222 ssh-ed25519 AAAAC3' },
        });
        expect(JSON.stringify(sent.metadata)).not.toContain('PRIVATE KEY');
    });
    it('stores custom public fields and secret fields separately', async () => {
        renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
        fireEvent.click(screen.getByRole('button', { name: 'Custom' }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByText('Custom')).toBeTruthy();
        expect(screen.queryByLabelText('Authentication')).toBeNull();
        expect(screen.queryByLabelText('API key')).toBeNull();
        fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Deployer' } });
        fireEvent.change(screen.getByLabelText('Custom type'), { target: { value: 'deployment' } });
        fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
        fireEvent.change(screen.getByLabelText('Field name 1'), { target: { value: 'region' } });
        fireEvent.change(screen.getByLabelText('Value for field 1'), { target: { value: 'eu-west' } });
        fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
        fireEvent.change(screen.getByLabelText('Field name 2'), { target: { value: 'token' } });
        fireEvent.click(screen.getByLabelText('Store field 2 as a secret'));
        fireEvent.change(screen.getByLabelText('Secret for field 2'), { target: { value: 'hunter2' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(requestJson).toHaveBeenCalledWith('/accounts', 'POST', expect.objectContaining({
            kind: 'custom', service: 'custom', auth_type: 'none', secrets: { token: 'hunter2' },
            metadata: expect.objectContaining({ custom_type: 'deployment', field_schema: [{ name: 'region', secret: false }, { name: 'token', secret: true }], fields: { region: 'eu-west' } }),
        })));
    });
    it('requires a replacement value when a saved secret field is renamed', async () => {
        const custom = account({ id: 'deploy', name: 'Deployer', kind: 'custom', service: 'custom', protocol: 'custom', auth_type: 'none', capabilities: [], secret_fields: ['token'], metadata: { custom_type: 'deployment', field_schema: [{ name: 'token', secret: true }], fields: {} } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [custom] });
        renderInPaneColumn(<KeyManager />);
        fireEvent.click(await screen.findByRole('button', { name: `Edit Deployer` }));
        fireEvent.change(screen.getByLabelText('Field name 1'), { target: { value: 'api_token' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Enter a secret value for api_token'));
        expect(requestJson).not.toHaveBeenCalled();
    });
    it('fits the pane\'s column in the list: one column of cards, the edit controls wrap under a long entry (CP-6)', async () => {
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' }, configured: false, model_configuration_error: 'codex cannot use an API key account' });
        const gh = account({ id: 'gh', name: 'GitHub', kind: 'git', service: 'github', protocol: 'github', endpoint: 'https://github.com', capabilities: ['git'], metadata: { author_name: 'Octo', author_email: 'octo@example.com' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({}), model] });
        const { body } = renderInPaneColumn(<KeyManager />);
        const fast = (await screen.findByRole('button', { name: 'Edit Fast' })).parentElement!;
        expect(screen.queryByRole('heading')).toBeNull();
        expect(body.querySelector('.grid')).toBeNull();
        // The child's Incompatible chip, Edit and trash wrap as controls;
        // Setup incomplete stays beside the name.
        expect(fast).toHaveClass('flex-wrap', 'min-w-0', 'ml-auto');
        expect(within(fast).getByText('Incompatible')).toBeInTheDocument();
        expect(within(fast).getByRole('button', { name: 'Edit Fast' })).toHaveTextContent('Edit');
        expect(within(fast).queryByText('Setup incomplete')).toBeNull();
        expect(within(fast.parentElement!).getByText('Fast').parentElement).toContainElement(
            within(fast.parentElement!).getByText('Setup incomplete'),
        );
        expect(fast.parentElement).toHaveClass('flex-wrap');
        expect(screen.getByRole('button', { name: 'Edit Primary' }).parentElement).toHaveClass('flex-wrap', 'min-w-0');
        fireEvent.click(screen.getByRole('button', { name: 'Delete Primary' }));
        expect(screen.getByRole('button', { name: 'Keep' }).parentElement).toHaveClass('flex-wrap');
        expectFitsPaneColumn(body);
        expectButtonRowsWrap(body);
        cleanup();
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({}), model, gh] });
        const again = renderInPaneColumn(<KeyManager />);
        await screen.findByRole('button', { name: 'Edit GitHub' });
        expectFitsPaneColumn(again.body);
        expectButtonRowsWrap(again.body);
    });

    it('fits the pane\'s column in the add card: the type segments and the actions wrap, the service under its label (CP-6)', async () => {
        const { body } = renderInPaneColumn(<KeyManager />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
        fireEvent.click(screen.getByRole('button', { name: 'Supported service' }));
        const segments = screen.getByRole('group', { name: 'Account type' });
        expect(segments).toHaveClass('flex-wrap');
        for (const option of within(segments).getAllByRole('button')) expect(option).toHaveClass('grow', 'whitespace-nowrap');
        expect(expectStackedFields(body)).toBe(1);
        expect(expectButtonRowsWrap(body)).toBeGreaterThanOrEqual(2);
        expectFitsPaneColumn(body);
    });

    it('fits the pane\'s column in every detail form: each field under its label, every button row wrapping (CP-6)', async () => {
        const custom = account({ id: 'deploy', name: 'Deployer', kind: 'custom', service: 'custom', protocol: 'custom', auth_type: 'none', capabilities: [], metadata: { custom_type: 'deployment', field_schema: [{ name: 'region', secret: false }], fields: { region: 'eu' } } });
        const gh = account({ id: 'gh', name: 'GitHub', kind: 'git', service: 'github', protocol: 'github', endpoint: 'https://github.com', capabilities: ['git'], metadata: { author_name: 'Octo', author_email: 'octo@example.com' } });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({}), custom, gh] });
        vi.mocked(requestJson).mockResolvedValue({ models: [] });
        const { body } = renderInPaneColumn(<KeyManager />);
        const fits = (fields: number) => {
            expect(expectStackedFields(body)).toBe(fields);
            expectFitsPaneColumn(body);
            expectButtonRowsWrap(body);
        };
        // A supported account on the app login: authentication, app,
        // subscription, the login document, the protocol and server URL
        // under Advanced, and the label.
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'cli' } });
        fits(7);
        expect(screen.getByRole('button', { name: 'Verify' }).parentElement).toHaveClass('flex-wrap');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // A model entry: name, harness, model, capabilities. Get current
        // models sits under the model field and wraps with its count.
        fireEvent.click(await screen.findByRole('button', { name: 'Add model' }));
        await waitFor(() => expect(screen.getByRole('button', { name: 'Get current models' })).toBeEnabled());
        const model = screen.getByLabelText('Model');
        const discovery = screen.getByRole('button', { name: 'Get current models' }).parentElement!;
        expect(discovery).toHaveClass('flex-wrap');
        expect(model.compareDocumentPosition(discovery) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        fits(4);
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // A GitHub entry: authentication, the personal access token, the
        // Server URL in the body, the commit author name and email, and the
        // label (no Advanced settings).
        fireEvent.click(await screen.findByRole('button', { name: 'Edit GitHub' }));
        expect(screen.getByLabelText('Personal access token')).toBeInTheDocument();
        fits(6);
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        // A custom entry: the custom type, a field's name and value, the label.
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Deployer' }));
        expect(screen.getByRole('button', { name: 'Remove field 1' }).parentElement).toHaveClass('flex-wrap');
        fits(4);
    });

    it('the overlay stays a dialog around the same one column (CP-6)', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({})] });
        render(<KeyManagerOverlay onClose={vi.fn()} />);
        const overlay = screen.getByRole('dialog', { name: 'Key manager' });
        expect(overlay).toHaveAttribute('aria-modal', 'true');
        expect(within(overlay).getByRole('heading', { name: 'Key manager' })).toBeInTheDocument();
        await screen.findByRole('button', { name: 'Edit Primary' });
        expect(overlay.querySelector('.grid')).toBeNull();
        expect(overlay.firstElementChild!.className).not.toMatch(/max-w-(?:lg|xl|\dxl)/);
    });

    it('signals an unconfigured account on its list card', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({ configured: false })] });
        renderInPaneColumn(<KeyManager />);
        const section = await screen.findByRole('region', { name: 'Primary account' });
        const flag = within(section).getByText('Setup incomplete');
        expect(flag).toHaveClass('text-teal', 'bg-teal-soft');
        expect(within(section).getByText('Primary').parentElement).toContainElement(flag);
        const edit = within(section).getByRole('button', { name: 'Edit Primary' });
        expect(edit).toHaveTextContent('Edit');
        expect(edit).not.toContainElement(flag);
    });

    it('paints the list, an incompatible model and the delete strip from the skin tokens (S04)', async () => {
        const model = account({ id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: 'primary', metadata: { harness: 'codex' }, model_configuration_error: 'codex cannot use an API key account' });
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({}), model] });
        const { container } = renderInPaneColumn(<KeyManager />);
        const chip = await screen.findByText('Incompatible');
        expect(chip).toHaveClass('text-red', 'bg-red-soft');
        expect(screen.getByText('codex cannot use an API key account')).toHaveClass('text-red');
        expect(screen.getByRole('region', { name: 'Primary account' })).toHaveClass('bg-sheet', 'border-line');
        fireEvent.click(screen.getByRole('button', { name: 'Delete Primary' }));
        expect(screen.getByRole('button', { name: 'Delete Primary' })).toHaveClass('hover:text-red');
        expect(screen.getByRole('button', { name: 'Delete' })).toHaveClass('text-red', 'hover:bg-red-soft');
        const painted = [...container.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('bg-black', 'rgba?\\('));
    });

    it('reports a pass in green and lifts the overlay on the shared dialog plate (S04)', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [account({})] });
        render(<KeyManagerOverlay onClose={vi.fn()} />);
        const overlay = screen.getByRole('dialog', { name: 'Key manager' });
        expect(overlay).toHaveClass('bg-(--lift)');
        expect(overlay.firstElementChild).toHaveClass('bg-sheet', 'border-line-2', 'shadow-(--shadow)');
        fireEvent.click(await screen.findByRole('button', { name: 'Edit Primary' }));
        fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
        expect(await screen.findByText('Verified')).toHaveClass('text-green');
    });

    it('owns the keyboard, so canvas chords do not fire behind the dialog', async () => {
        render(<KeyManagerOverlay onClose={vi.fn()} />);
        expect(screen.getByRole('dialog', { name: 'Key manager' }).hasAttribute('data-owns-keyboard')).toBe(true);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
    });
});


it('uses the API default harness and its display label __F0951', async () => {
    const parent = account({ id: 'parent', name: 'Parent', supported_harnesses: [] });
    vi.mocked(fetchAccounts).mockResolvedValue({ ...response, accounts: [parent],
        default_model_harness: 'future', model_harnesses: ['future'], harness_labels: { future: 'Future engine' } });
    renderInPaneColumn(<KeyManager />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add model' }));
    expect(screen.getByLabelText('Harness')).toHaveValue('future');
    expect(screen.getByRole('option', { name: 'Future engine (unavailable)' })).toBeTruthy();
});
