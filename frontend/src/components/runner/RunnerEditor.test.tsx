import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { RunnerEditor } from './RunnerEditor';
import editorSource from './RunnerEditor.tsx?raw';
import fieldSource from './FieldControl.tsx?raw';
import { fetchRunnerConfig, fetchRunnerVocabulary, type RunnerGenerator, type RunnerDraft } from '@/lib/runnerApi';
import { fetchAccounts } from '@/lib/accountsApi';
import { KEYBOARD_OWNER_ATTR, eventPathHasAttr } from '@/lib/keyboardOwner';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

vi.mock('@/lib/accountsApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchAccounts: vi.fn(async () => ({ accounts: [], services: [], status: { available: true } })) }));

vi.mock('@/lib/runnerApi', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchRunnerConfig: vi.fn(),
    fetchRunnerVocabulary: vi.fn(async () => ({
        enums: { docker: ['none', 'limited', 'full'], internet: ['none', 'proxy', 'full'], commands: ['deny', 'ask', 'allow'] },
        defaults: { docker: 'none', internet: 'full', commands: 'deny', update_packages: true, internet_allow: [] },
        labels: { docker: 'Docker', internet: 'Internet access', commands: 'Commands', update_packages: 'Allow package updates', internet_allow: 'Allowed internet hosts' }, descriptions: {},
    })),
}));
vi.mock('@/components/settings/KeyManager', () => ({ KeyManagerOverlay: ({ onClose }: { onClose: () => void }) => <button onClick={onClose}>Close Key manager</button> }));

const generator: RunnerGenerator = {
    id: 'writer', name: 'Writer', main_config: 'settings.set_yaml',
    fields: [{ key: 'title', label: 'Title', type: 'text' }, { key: 'depth', label: 'Depth', type: 'number', advanced: true }],
    settings_files: {}, account_requirements: {}, config_folders: [], enforces: [],
    prompt_intent: 'Work on current cell', configs: [{ id: 'default', name: 'Default', revision: 'default-rev' }, { id: 'other', name: 'Other', revision: 'other-rev' }],
    default: { runner: { name: 'Default writer', description: 'Original', tags: ['writing'], prompt: 'Read the cell', settings: { 'settings.set_yaml': { title: 'Default title', depth: 1 } }, accounts: {}, folders: [], permissions: {}, packages: {} }, files: [{ name: 'old.txt', content: 'old' }] },
};
const initial: RunnerDraft = { runner: { ...generator.default.runner, id: 'writer-1', generator: 'writer' }, files: generator.default.files, revision: 'rev1' };
// A generator that enforces the commands axis itself (`enforces: [commands]`).
const enforcing: RunnerGenerator = { ...generator, enforces: ['commands'] };
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());
describe('Shared RunnerEditor', () => {
    it('states only selected visible dropdown descriptions at commands allow (P4R05)', async () => {
        const configured: RunnerGenerator = { ...enforcing, fields: [
            { key: 'engine', type: 'dropdown', label: 'Engine', default: 'quiet', options: [
                { value: 'quiet', label: 'Quiet' },
                { value: 'exposed', label: 'Exposed', description: 'Permitted commands can read the fixture credential.' },
                { value: 'unused', label: 'Unused', description: 'Unselected option disclosure.' },
            ] },
            { key: 'concealed', type: 'dropdown', show_if: { engine: 'never' }, options: [
                { value: 'hidden', label: 'Hidden', description: 'Hidden field disclosure.' },
            ] },
        ] };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, settings: { 'settings.set_yaml': { engine: 'exposed', concealed: 'hidden' } } } }} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        const commands = await screen.findByLabelText('Commands');
        expect(screen.queryByText(/Permitted commands can read the fixture credential/)).toBeNull();
        fireEvent.change(commands, { target: { value: 'ask' } });
        expect(screen.queryByText(/Permitted commands can read the fixture credential/)).toBeNull();
        fireEvent.change(commands, { target: { value: 'allow' } });
        const disclosure = screen.getByText(/Permitted commands can read the fixture credential/);
        expect(within(commands.closest('label')!).getByRole('note')).toBe(disclosure);
        expect(screen.queryByText(/Unselected option disclosure/)).toBeNull();
        expect(screen.queryByText(/Hidden field disclosure/)).toBeNull();
        fireEvent.change(commands, { target: { value: 'deny' } });
        expect(screen.queryByText(/Permitted commands can read the fixture credential/)).toBeNull();
        fireEvent.change(commands, { target: { value: 'allow' } });
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        fireEvent.change(screen.getByLabelText('Engine'), { target: { value: 'quiet' } });
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        expect(screen.queryByText(/Permitted commands can read the fixture credential/)).toBeNull();
    });
    it('uses effective account-derived option descriptions for an omitted allow default (P4R05)', async () => {
        const account = { id: 'sensitive', name: 'Sensitive connection', kind: 'model', service: 'fake', protocol: 'fake', endpoint: '', model: 'fixture', capabilities: ['text'], auth_type: 'api_key', metadata: { mode: 'exposed' }, configured: true };
        vi.mocked(fetchAccounts).mockResolvedValueOnce({ accounts: [account, { ...account, id: 'quiet', name: 'Quiet connection', metadata: { mode: 'quiet' } }], services: [], plan_logins: {}, model_harnesses: [], default_model_harness: 'fixture', harness_labels: {}, login_plans: {}, status: { available: true, config_path: '' } });
        vi.mocked(fetchRunnerVocabulary).mockResolvedValueOnce({
            enums: { docker: ['none'], internet: ['full'], commands: ['deny', 'ask', 'allow'] },
            defaults: { docker: 'none', internet: 'full', commands: 'allow', update_packages: true, internet_allow: [] },
            labels: { docker: 'Docker', internet: 'Internet access', commands: 'Commands', update_packages: 'Allow package updates', internet_allow: 'Allowed internet hosts' }, descriptions: {},
        });
        const configured: RunnerGenerator = { ...enforcing, account_requirements: { connection: { label: 'Connection', kind: 'model' } }, fields: [
            { key: 'mode', type: 'dropdown', label: 'Mode', default: 'quiet', account_role: 'connection', account_property: 'metadata.mode', options: [
                { value: 'quiet', label: 'Quiet' }, { value: 'exposed', label: 'Exposed', description: 'The selected connection exposes a fixture credential to permitted commands.' },
            ] },
        ] };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, accounts: { connection: account.id }, settings: { 'settings.set_yaml': { mode: 'quiet' } } } }} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        await screen.findByRole('option', { name: /Sensitive connection/ });
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        expect(await screen.findByLabelText('Commands')).toHaveValue('allow');
        expect(screen.getByText(/The selected connection exposes a fixture credential/)).toBeTruthy();
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        fireEvent.change(screen.getByLabelText('Connection'), { target: { value: 'quiet' } });
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        expect(screen.queryByText(/The selected connection exposes a fixture credential/)).toBeNull();
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        fireEvent.change(screen.getByLabelText('Connection'), { target: { value: '' } });
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        expect(screen.queryByText(/The selected connection exposes a fixture credential/)).toBeNull();
    });
    it('does not show option disclosures for an unenforced saved commands grant (P4R05)', async () => {
        const configured: RunnerGenerator = { ...generator, fields: [
            { key: 'mode', type: 'dropdown', options: [{ value: 'exposed', label: 'Exposed', description: 'Irrelevant fixture disclosure.' }] },
        ] };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, permissions: { commands: 'allow' }, settings: { 'settings.set_yaml': { mode: 'exposed' } } } }} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        expect(await screen.findByText(/not enforced by this generator/)).toBeTruthy();
        expect(screen.queryByText(/Irrelevant fixture disclosure/)).toBeNull();
    });

    it('opens on a generator-only card and expands into the tabbed form', async () => {
        render(<RunnerEditor generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        expect(screen.queryByRole('tab', { name: 'Config' })).toBeNull();
        expect(screen.queryByLabelText('Name')).toBeNull();
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Generator'), { target: { value: 'writer' } });
        expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByLabelText('Name')).toHaveValue('');
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'My runner' } });
        expect(screen.getByLabelText('Title')).toHaveValue('Default title');
        expect(screen.getByRole('tab', { name: 'Config' })).toHaveAttribute('aria-selected', 'true');
        expect(screen.queryByLabelText('Depth')).toBeNull();
        fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
        expect(screen.getByLabelText('Depth')).toBeTruthy();
        expect(screen.queryByLabelText('Title')).toBeNull();
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        expect(screen.getByLabelText('Contents of old.txt')).toBeTruthy();
        expect(screen.queryByText('settings.set_yaml')).toBeNull();
        await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled());
    });
    it('saves a draft built from a generator template as a new runner under a slug of its name', async () => {
        // The manage view opens Add runner with a generator preselected this
        // way: an unrevisioned draft with no id.
        const save = vi.fn();
        const template: RunnerDraft = { runner: { ...generator.default.runner, id: '', generator: 'writer', name: '', description: 'Carried over' }, files: generator.default.files };
        render(<RunnerEditor initial={template} generators={[generator]} onSave={save} onCancel={vi.fn()} />);
        expect(screen.queryByLabelText('Generator')).toBeNull();
        expect(screen.getByLabelText('Description')).toHaveValue('Carried over');
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Typed runner' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ revision: undefined,
            runner: expect.objectContaining({ name: 'Typed runner', generator: 'writer', id: expect.stringMatching(/^typed-runner-/) }) })));
    });
    it('holds Save until the runner is named, then saves under a slug of that name', async () => {
        const save = vi.fn();
        render(<RunnerEditor generators={[generator]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.change(screen.getByLabelText('Generator'), { target: { value: 'writer' } });
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: '   ' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'My runner' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
            runner: expect.objectContaining({ name: 'My runner', generator: 'writer', id: expect.stringMatching(/^my-runner-/) }) })));
    });
    it('keeps an existing runner unsavable while its name is cleared', () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: '' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Renamed' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });
    it('offers only Cancel and Save as footer actions', () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        // The generator is a label; file sets belong to the Files tab.
        expect(screen.getByText('Generator · Writer').tagName).toBe('SPAN');
        expect(screen.queryByRole('button', { name: 'Load file sets' })).toBeNull();
        expect(screen.queryByRole('button', { name: /Push into config/ })).toBeNull();
        expect(screen.queryByRole('button', { name: /Save and run/ })).toBeNull();
        expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
        // Duplicate and Delete are the manage view's runner-row actions.
        const footer = screen.getByRole('button', { name: 'Save' }).parentElement!;
        expect(within(footer).getAllByRole('button').map(b => b.textContent)).toEqual(['Cancel', 'Save']);
    });
    it('edits tags as chips', async () => {
        const save = vi.fn();
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: 'Remove tag writing' }));
        fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'drafting' } });
        fireEvent.keyDown(screen.getByLabelText('Tags'), { key: 'Enter' });
        fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'review,' } });
        expect(screen.getByRole('button', { name: 'Remove tag review' })).toBeTruthy();
        fireEvent.keyDown(screen.getByLabelText('Tags'), { key: 'Backspace' });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ tags: ['drafting'] }) })));
    });
    it('splits a pasted comma list into separate tags', () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'a,b,c' } });
        expect(screen.getByRole('button', { name: 'Remove tag a' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Remove tag b' })).toBeTruthy();
        expect(screen.queryAllByRole('button', { name: /Remove tag .*,/ })).toHaveLength(0);
        expect(screen.getByLabelText('Tags')).toHaveValue('c');
        fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'foo,' } });
        expect(screen.getByRole('button', { name: 'Remove tag foo' })).toBeTruthy();
        expect(screen.getByLabelText('Tags')).toHaveValue('');
    });
    it('keeps a typed newline in the allowed hosts and trims the lines on save', async () => {
        const save = vi.fn();
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        const hosts = screen.getByLabelText('Allowed internet hosts');
        fireEvent.change(hosts, { target: { value: 'a.com\n' } });
        expect(hosts).toHaveValue('a.com\n');
        fireEvent.change(hosts, { target: { value: 'a.com\n b.com \n\n' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
            runner: expect.objectContaining({ permissions: expect.objectContaining({ internet_allow: ['a.com', 'b.com'] }) }) })));
    });
    it('explains the allowed-host grammar and names entries that never match (F2041)', async () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        expect(screen.getByText(/example\.com allows api\.example\.com/)).toBeTruthy();
        const hosts = screen.getByLabelText('Allowed internet hosts');
        fireEvent.change(hosts, { target: { value: ' api.example.com \n.Example.COM' } });
        expect(screen.queryByText(/never match/)).toBeNull();
        fireEvent.change(hosts, { target: { value: 'a.com\n*.example.com\nhttps://b.com\nc.com:443' } });
        expect(screen.getByRole('note').textContent).toContain('never match: *.example.com, https://b.com, c.com:443.');
    });
    it('loads custom files after confirmation while preserving runner settings and hidden settings files', async () => {
        const settingsGenerator = { ...generator, settings_files: { 'settings.set_yaml': 'schema', 'extra.set_yaml': 'extra schema' } };
        const configured = { ...initial, runner: { ...initial.runner, accounts: { connection: 'bound-account' }, permissions: { docker: 'none', commands: 'ask' }, packages: { fixture: 'pinned' } },
            files: [...initial.files, { name: 'settings.set_yaml', content: 'existing main settings' }, { name: 'extra.set_yaml', content: 'existing extra settings' }] };
        vi.mocked(fetchRunnerConfig).mockResolvedValue({ runner: { ...generator.default.runner, name: 'Template name', description: 'From config', tags: [], prompt: '', accounts: {}, permissions: { docker: 'limited' } }, files: [{ name: 'replacement.txt', content: 'new' }, { name: 'settings.set_yaml', content: 'replacement main settings' }, { name: 'extra.set_yaml', content: 'replacement extra settings' }] });
        const save = vi.fn();
        render(<RunnerEditor initial={configured} generators={[settingsGenerator]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        fireEvent.click(screen.getByRole('button', { name: 'Load file sets' }));
        expect(screen.getByText("Loading a file set replaces this runner's custom files.")).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled();
        fireEvent.click(screen.getByRole('radio', { name: 'Other' }));
        fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
        await screen.findByLabelText('Contents of replacement.txt');
        expect(fetchRunnerConfig).toHaveBeenCalledWith('writer', 'other');
        expect(screen.getByLabelText('Name')).toHaveValue('Default writer');
        expect(screen.getByLabelText('Description')).toHaveValue('Original');
        expect(screen.queryByLabelText('Contents of old.txt')).toBeNull();
        expect(screen.queryByLabelText('Contents of settings.set_yaml')).toBeNull();
        expect(screen.queryByLabelText('Contents of extra.set_yaml')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith({ ...configured, files: [
            { name: 'settings.set_yaml', content: 'existing main settings' },
            { name: 'extra.set_yaml', content: 'existing extra settings' },
            { name: 'replacement.txt', content: 'new' },
        ] }));
    });
    it('Cancel discards field and file edits without writing', async () => {
        const save = vi.fn(), cancel = vi.fn();
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={save} onCancel={cancel} />);
        fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Changed' } });
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        fireEvent.click(screen.getByRole('button', { name: 'Delete file' }));
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
        expect(cancel).toHaveBeenCalledOnce(); expect(save).not.toHaveBeenCalled();
        expect(initial.files).toEqual([{ name: 'old.txt', content: 'old' }]);
    });
    it('keeps object settings typed and blocks invalid JSON until corrected', async () => {
        const withObject = { ...generator, fields: [{ key: 'options', label: 'Options', type: 'object' as const }], default: { ...generator.default, runner: { ...generator.default.runner, settings: { 'settings.set_yaml': { options: { nested: { enabled: true }, count: 2 } } } } } };
        const save = vi.fn();
        render(<RunnerEditor initial={{ ...initial, runner: { ...withObject.default.runner, id: 'writer-1', generator: 'writer' } }} generators={[withObject]} onSave={save} onCancel={vi.fn()} />);
        expect(JSON.parse((screen.getByLabelText('Options') as HTMLTextAreaElement).value)).toEqual({ nested: { enabled: true }, count: 2 });
        fireEvent.change(screen.getByLabelText('Options'), { target: { value: '{invalid' } });
        expect(screen.getByLabelText('Options')).toHaveAttribute('aria-invalid', 'true');
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Options'), { target: { value: '[1, 2]' } });
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(screen.getByLabelText('Options'), { target: { value: '{"nested":{"enabled":false},"items":[1,2]}' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ settings: { 'settings.set_yaml': { options: { nested: { enabled: false }, items: [1, 2] } } } }) })));
    });
    it('replaces a nested supplemental file with binary bytes while retaining its path', async () => {
        const save = vi.fn();
        const withFolder = { ...generator, config_folders: [{ name: 'assets' }] };
        const initialFile = { ...initial, files: [{ name: 'assets/logo.bin', content: '', blob_hash: 'old-hash' }] };
        render(<RunnerEditor initial={initialFile} generators={[withFolder]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        fireEvent.change(screen.getByLabelText('Replace assets/logo.bin'), { target: { files: [{ name: 'replacement.bin', arrayBuffer: async () => new Uint8Array([0, 255, 2]).buffer }] } });
        await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ files: [{ name: 'assets/logo.bin', content: 'AP8C', encoding: 'base64' }], runner: expect.objectContaining({ folders: ['assets'] }) })));
    });
    it('shows a key requirement and a route to the Key manager when no account qualifies', async () => {
        const configured: RunnerGenerator = { ...generator, account_requirements: { model: { label: 'Agent model', kind: 'model', capabilities: ['text'] } } };
        render(<RunnerEditor initial={initial} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
        expect(screen.getByRole('button', { name: 'Agent model requirement' })).toHaveAttribute('title', 'Type: model · Capabilities: text');
        expect(screen.getByText('No valid account')).toBeTruthy();
        expect(screen.getByText('This runner saves, but cannot run until you set: Agent model.')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Open Key manager' }));
        fireEvent.click(screen.getByRole('button', { name: 'Close Key manager' }));
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });
    it('groups valid model subitems by account and derives the harness for settings and save', async () => {
        const parent = { id: 'work', name: 'Work account', kind: 'account', service: 'openai', protocol: 'openai', endpoint: 'https://api.example/v1', model: '', capabilities: ['text'], auth_type: 'api_key', metadata: {}, configured: true };
        const model = { ...parent, id: 'fast', name: 'Fast', kind: 'model', model: 'fast-model', credential_account_id: parent.id, metadata: { harness: 'codex' } };
        vi.mocked(fetchAccounts).mockResolvedValue({ accounts: [parent, model, { ...model, id: 'incomplete', name: 'Incomplete', model: '', model_configuration_error: 'Choose a model — this endpoint has no default model.' }, { ...model, id: 'unsupported', name: 'Unsupported', protocol: 'other' }], services: [], plan_logins: {}, model_harnesses: [], default_model_harness: 'pydantic-ai', harness_labels: {}, login_plans: {}, status: { available: true, config_path: '' } });
        const configured: RunnerGenerator = { ...generator, account_requirements: { model: { label: 'Agent model', kind: 'model', protocols: ['openai'], capabilities: ['text'] } }, fields: [{ key: 'backend', type: 'dropdown', label: 'Harness', default: 'pydantic-ai', account_role: 'model', account_property: 'metadata.harness' }, { key: 'terminal', type: 'text', label: 'Terminal options', show_if: { backend: 'codex' } }] };
        const save = vi.fn();
        render(<RunnerEditor initial={initial} generators={[configured]} onSave={save} onCancel={vi.fn()} />);
        expect(await screen.findByRole('group', { name: 'Work account' })).toBeTruthy();
        expect(screen.queryByRole('option', { name: /Incomplete/ })).toBeNull();
        expect(screen.queryByRole('option', { name: /Unsupported/ })).toBeNull();
        fireEvent.change(screen.getByLabelText('Agent model'), { target: { value: 'fast' } });
        expect(screen.getByLabelText('Terminal options')).toBeTruthy();
        expect(screen.queryByRole('combobox', { name: 'Harness' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ accounts: { model: 'fast' }, settings: { 'settings.set_yaml': expect.objectContaining({ backend: 'codex' }) } }) })));
    });
    it('keeps a missing saved connection visible without silently replacing it', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ accounts: [], services: [], plan_logins: {}, model_harnesses: [], default_model_harness: 'pydantic-ai', harness_labels: {}, login_plans: {}, status: { available: true, config_path: '' } });
        const configured: RunnerGenerator = { ...generator, account_requirements: { git: { label: 'Git account', kind: 'git', capabilities: ['git'], required: false } } };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, accounts: { git: 'deleted' } } }} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        expect(screen.getByLabelText('Git account')).toHaveValue('deleted');
        expect(screen.getByRole('option', { name: 'Unavailable account (deleted)' })).toBeDisabled();
        expect(screen.getByText('This runner saves, but cannot run until you set: Git account.')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Git account'), { target: { value: '' } });
        expect(screen.queryByText(/cannot run until you set/)).toBeNull();
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalled());
    });
    it('rechecks conditional options when an account supplies a different harness', async () => {
        const account = { id: 'api-model', name: 'Direct model', kind: 'model', service: 'openai', protocol: 'openai', endpoint: '', model: 'model', capabilities: ['text'], auth_type: 'api_key', metadata: { harness: 'pydantic-ai' }, configured: true };
        vi.mocked(fetchAccounts).mockResolvedValue({ accounts: [account], services: [], plan_logins: {}, model_harnesses: [], default_model_harness: 'pydantic-ai', harness_labels: {}, login_plans: {}, status: { available: true, config_path: '' } });
        const configured: RunnerGenerator = { ...generator, account_requirements: { model: { kind: 'model' } }, fields: [
            { key: 'backend', type: 'dropdown', options: [{ value: 'pydantic-ai', label: 'Direct API' }, { value: 'codex', label: 'Codex' }], default: 'pydantic-ai', account_role: 'model', account_property: 'metadata.harness' },
            { key: 'mode', type: 'dropdown', label: 'Mode', options: [{ value: 'basic', label: 'Basic' }, { value: 'advanced', label: 'Advanced', enable_if: { backend: 'codex' } }] },
        ] };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, accounts: { model: account.id }, settings: { 'settings.set_yaml': { backend: 'codex', mode: 'advanced' } } } }} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        await screen.findByText('This runner saves, but cannot run until you set: Mode.');
        fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'basic' } });
        expect(screen.queryByText(/cannot run until you set/)).toBeNull();
    });
    it('shows the prompt only while the generator gate opens it', () => {
        const gated: RunnerGenerator = { ...generator, prompt_show_if: { session: 'oneshot' }, prompt_required_if: { session: 'oneshot' }, prompt_required: false,
            fields: [{ key: 'session', type: 'dropdown', label: 'Session', options: [{ value: 'oneshot', label: 'One-shot' }, { value: 'interactive', label: 'Interactive' }], default: 'oneshot' }] };
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, prompt: '', settings: { 'settings.set_yaml': { session: 'oneshot' } } } }} generators={[gated]} onSave={vi.fn()} onCancel={vi.fn()} />);
        expect(screen.getByLabelText('Prompt')).toBeTruthy();
        expect(screen.getByText('Prompt *')).toBeTruthy();
        expect(screen.getByText('This runner saves, but cannot run until you set: Prompt.')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Session'), { target: { value: 'interactive' } });
        expect(screen.queryByLabelText('Prompt')).toBeNull();
        expect(screen.queryByText(/cannot run until you set/)).toBeNull();
    });
    it('keeps the prompt while the gate hides it', async () => {
        const gated: RunnerGenerator = { ...generator, prompt_show_if: { session: 'oneshot' }, prompt_required: false,
            fields: [{ key: 'session', type: 'dropdown', label: 'Session', options: [{ value: 'oneshot', label: 'One-shot' }, { value: 'interactive', label: 'Interactive' }], default: 'oneshot' }] };
        const save = vi.fn();
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, prompt: 'Summarize this cell', settings: { 'settings.set_yaml': { session: 'oneshot' } } } }} generators={[gated]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.change(screen.getByLabelText('Session'), { target: { value: 'interactive' } });
        expect(screen.queryByLabelText('Prompt')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ prompt: 'Summarize this cell' }) })));
        fireEvent.change(screen.getByLabelText('Session'), { target: { value: 'oneshot' } });
        expect(screen.getByLabelText('Prompt')).toHaveValue('Summarize this cell');
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
        expect(save.mock.calls[1][0].runner.prompt).toBe('Summarize this cell');
    });
    it('keeps Escape inside the file-set modal instead of tearing the editor down', async () => {
        const cancel = vi.fn();
        render(<div onKeyDown={e => { if (e.key === 'Escape') cancel(); }}>
            <RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />
        </div>);
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        fireEvent.click(screen.getByRole('button', { name: 'Load file sets' }));
        const dialog = screen.getByRole('dialog', { name: 'Load file sets' });
        await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
        // The scene's window capture handler must see the dialog own the key.
        const seen = vi.fn();
        const listener = (e: Event) => seen(eventPathHasAttr(e, KEYBOARD_OWNER_ATTR));
        window.addEventListener('keydown', listener, true);
        fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' });
        window.removeEventListener('keydown', listener, true);
        expect(seen).toHaveBeenCalledWith(true);
        expect(cancel).not.toHaveBeenCalled();
        expect(screen.queryByRole('dialog', { name: 'Load file sets' })).toBeNull();
        expect(screen.getByLabelText('Description')).toBeTruthy();
    });
    it('names each tab panel from its tab', async () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        const panel = screen.getByRole('tabpanel');
        expect(panel.id).toBe('runner-panel-config');
        expect(screen.getByRole('tab', { name: 'Config' })).toHaveAttribute('aria-controls', 'runner-panel-config');
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        expect(screen.getByRole('tabpanel', { name: 'Security' }).id).toBe('runner-panel-security');
    });
    it('offers the Commands permission, falling back to deny, and saves the choice', async () => {
        const save = vi.fn();
        render(<RunnerEditor initial={initial} generators={[enforcing]} onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        const commands = screen.getByLabelText('Commands');
        expect(commands).toHaveValue('deny');
        expect([...(commands as HTMLSelectElement).options].map(o => o.value)).toEqual(['deny', 'ask', 'allow']);
        expect(screen.getByText(/ask requests approval; allow runs commands unattended/)).toBeTruthy();
        fireEvent.change(commands, { target: { value: 'ask' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
            runner: expect.objectContaining({ permissions: expect.objectContaining({ commands: 'ask' }) }) })));
    });
    it('reads the Commands levels from the vocabulary API', async () => {
        vi.mocked(fetchRunnerVocabulary).mockResolvedValueOnce({
            enums: { docker: ['none'], internet: ['full'], commands: ['deny', 'allow'] },
            defaults: { docker: 'none', internet: 'full', commands: 'deny', update_packages: true, internet_allow: [] },
            labels: { docker: 'Docker', internet: 'Internet access', commands: 'Commands', update_packages: 'Allow package updates', internet_allow: 'Allowed internet hosts' }, descriptions: {},
        });
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, permissions: { commands: 'allow' } } }} generators={[enforcing]}
            onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        const commands = screen.getByLabelText('Commands') as HTMLSelectElement;
        expect(commands).toHaveValue('allow');
        expect([...commands.options].map(o => o.value)).toEqual(['deny', 'allow']);
    });
    it('offers no Commands level for a generator that does not enforce it', async () => {
        render(<RunnerEditor initial={initial} generators={[generator]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        expect(screen.queryByLabelText('Commands')).toBeNull();
        expect(screen.queryByText(/not enforced by this generator/)).toBeNull();
        expect(screen.getByLabelText('Docker')).toBeTruthy();
    });
    it('notes a saved Commands level its generator does not enforce, and keeps it on save', async () => {
        const save = vi.fn();
        render(<RunnerEditor initial={{ ...initial, runner: { ...initial.runner, permissions: { commands: 'allow' } } }} generators={[generator]}
            onSave={save} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Docker');
        expect(screen.queryByLabelText('Commands')).toBeNull();
        expect(screen.getByRole('note')).toHaveTextContent('Commands: allow — not enforced by this generator, so it has no effect.');
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
            runner: expect.objectContaining({ permissions: { commands: 'allow' } }) })));
    });
    it('reports an empty Advanced tab', () => {
        render(<RunnerEditor initial={initial} generators={[{ ...generator, fields: [generator.fields[0]] }]} onSave={vi.fn()} onCancel={vi.fn()} />);
        fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
        expect(screen.getByText('No advanced settings for this generator.')).toBeTruthy();
    });
});


it('uses served permission levels, labels and defaults for a new form __F0948', async () => {
    vi.mocked(fetchRunnerVocabulary).mockResolvedValueOnce({
        enums: { docker: ['limited'], internet: ['none'], commands: ['ask'] },
        defaults: { docker: 'limited', internet: 'none', commands: 'ask', update_packages: false, internet_allow: ['test.invalid'] },
        labels: { docker: 'Placement', internet: 'Network', commands: 'Commands', update_packages: 'Updates', internet_allow: 'Hosts' },
        descriptions: { docker: { limited: 'Runs in one container.' } },
    });
    render(<RunnerEditor generators={[enforcing]} onSave={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Generator'), { target: { value: 'writer' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Security' }));
        await screen.findByLabelText('Placement');
    expect(await screen.findByLabelText('Placement')).toHaveValue('limited');
    expect(screen.getByLabelText('Placement')).toHaveAccessibleDescription('Runs in one container.');
    expect(screen.getByLabelText('Network')).toHaveValue('none');
    expect(screen.getByLabelText('Commands')).toHaveValue('ask');
    expect(screen.getByLabelText('Updates')).not.toBeChecked();
    expect(screen.getByLabelText('Hosts')).toHaveValue('test.invalid');
    expect(screen.queryByRole('option', { name: 'full' })).toBeNull();
});

/** The retired palette and colour literals: none may come back (S04). */
const OLD_PALETTE = RETIRED_TOKENS;
const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(|\b(bg|text|border)-(white|black)\b|\b(slate|zinc|gray|neutral|stone)-\d/;

describe('RunnerEditor on the skin tokens (S04)', () => {
    it('every tab, the requirement warnings and the load dialog paint only skin tokens (S04)', async () => {
        vi.mocked(fetchAccounts).mockResolvedValue({ accounts: [], services: [], status: { available: true } } as never);
        const configured: RunnerGenerator = { ...generator, account_requirements: { model: { label: 'Agent model', kind: 'model', capabilities: ['text'] } } };
        render(<RunnerEditor initial={initial} generators={[configured]} onSave={vi.fn()} onCancel={vi.fn()} />);
        // Blocking problems are red; the open tab carries the teal rule.
        expect((await screen.findByText('No valid account')).className).toContain('text-red');
        expect(screen.getByText('This runner saves, but cannot run until you set: Agent model.').className).toContain('text-red');
        expect(screen.getByRole('tab', { name: 'Config' }).className).toContain('after:bg-teal');
        const editor = document.querySelector('[data-runner-editor]')!;
        expect(editor.className).toContain('text-ink');
        for (const name of ['Advanced', 'Files', 'Security', 'Config']) {
            expect(document.body.innerHTML).not.toMatch(OLD_PALETTE);
            expect(document.body.innerHTML).not.toMatch(COLOUR_LITERAL);
            fireEvent.click(screen.getByRole('tab', { name }));
        }
        await waitFor(() => expect(fetchRunnerVocabulary).toHaveBeenCalled());
        fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
        fireEvent.click(screen.getByRole('button', { name: 'Load file sets' }));
        const dialog = screen.getByRole('dialog', { name: 'Load file sets' });
        expect(dialog.className).toContain('bg-(--lift)');
        expect(screen.getByText(/Loading a file set replaces/).className).toContain('text-red');
        expect(document.body.innerHTML).not.toMatch(OLD_PALETTE);
        expect(document.body.innerHTML).not.toMatch(COLOUR_LITERAL);
    });

    it('the editor and its field control name no retired palette token and no colour literal (S04)', () => {
        for (const source of [editorSource, fieldSource]) {
            expect(source).not.toMatch(OLD_PALETTE);
            expect(source).not.toMatch(COLOUR_LITERAL);
        }
    });
});
