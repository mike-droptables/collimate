import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { NewTab } from './NewTab';
import { fetchSuggested, type SuggestedCatalog, type Suggestion } from '@/lib/api/suggested';
import { withSavedSuggestions, type SavedSuggestion } from '@/lib/api/userCatalog';
import { fetchAccounts, type Account, type AccountsPayload } from '@/lib/accountsApi';
import { fetchChannelOutline, fetchChannelTags } from '@/lib/api/cells';
import { expandZipForNewChannel, type ZipExpansion } from '@/lib/api/transfer';
import { exportZip } from '@/lib/transfer/exportSinks';
import { pickZip } from '@/lib/transfer/importSources';
import type { RunnerDraft, RunnerGenerator } from '@/lib/runnerApi';
import { retiredOr } from '@/test/retiredTokens';

vi.mock('@/lib/api/suggested', () => ({ fetchSuggested: vi.fn() }));
vi.mock('@/lib/api/userCatalog', async original => ({
    ...(await original<Record<string, unknown>>()), withSavedSuggestions: vi.fn(async (catalog: Promise<unknown>) => catalog),
}));
vi.mock('@/lib/accountsApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchAccounts: vi.fn() }));
vi.mock('@/lib/runnerApi', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchRunnerVocabulary: vi.fn(async () => ({ enums: {}, defaults: { docker: 'none', internet: 'full', commands: 'deny', update_packages: true, internet_allow: [] }, labels: {}, descriptions: {} })),
}));
vi.mock('@/lib/api/cells', async original => ({ ...(await original<Record<string, unknown>>()), fetchChannelOutline: vi.fn(), fetchChannelTags: vi.fn() }));
vi.mock('@/lib/api/transfer', async original => ({ ...(await original<Record<string, unknown>>()), expandZipForNewChannel: vi.fn() }));
vi.mock('@/lib/transfer/exportSinks', async original => ({ ...(await original<Record<string, unknown>>()), exportZip: vi.fn(async () => 'saved') }));
vi.mock('@/lib/transfer/importSources', async original => ({ ...(await original<Record<string, unknown>>()), pickZip: vi.fn() }));

const template = { name: '', description: '', tags: [], prompt: '', settings: { 'settings.set_yaml': {} }, accounts: {}, folders: [], permissions: {}, packages: {} };
const agent: RunnerGenerator = {
    id: 'agent', name: 'Agent', main_config: 'settings.set_yaml', fields: [], settings_files: {}, config_folders: [], enforces: [],
    prompt_label: 'Prompt', prompt_type: 'textarea',
    account_requirements: { model: { label: 'Agent model', kind: 'model', capabilities: ['text'] } },
    configs: [{ id: 'default', name: 'Default' }], default: { runner: template, files: [] },
};
const web: RunnerGenerator = { ...agent, id: 'web', name: 'Web', account_requirements: {}, configs: [{ id: 'default', name: 'Default' }, { id: 'docs', name: 'Docs' }] };
const draft = (id: string, generator: string, name: string): RunnerDraft => ({
    runner: { ...template, id, generator, name, description: `${name} runner`, prompt: 'Go' }, files: [{ name: 'notes.txt', content: 'hi' }],
});
const offered = (runner: RunnerDraft, quick_fields: Suggestion['runners'][number]['quick_fields'] = []) => ({ draft: runner, quick_fields });
const standard: Suggestion = {
    id: 'standard', name: 'Standard', description: 'Every shipped generator and renderer.',
    generators: [{ name: 'Agent', folder: 'generators/agent' }, { name: 'Web', folder: 'generators/web' }],
    renderers: [{ name: 'Documents', folder: 'renderers/documents' }], libraries: [], tutorial: [{ name: 'Welcome', folder: 'suggested/tutorial/welcome' }],
    runner_generators: [agent, web], runners: [
        offered(draft('agent-annotation', 'agent', 'Agent annotation'), [{ account: 'model' }]),
        offered(draft('web-default', 'web', 'Web default')),
    ],
};
const minimal: Suggestion = { ...standard, id: 'minimal', name: 'Minimal', description: 'Web only.', tutorial: [], runners: [offered(draft('web-default', 'web', 'Web only'))] };
const catalog = (patch: Partial<SuggestedCatalog> = {}): SuggestedCatalog => ({
    suggestions: [standard], error: null, ...patch,
});
const model: Account = { id: 'fast', name: 'Fast', kind: 'model', service: 'openai', protocol: 'openai', endpoint: '', model: 'fast', capabilities: ['text'], auth_type: 'api_key', metadata: {}, configured: true };
const accounts = (list: Account[]) => ({ accounts: list }) as unknown as AccountsPayload;
const entry = (path: string, blob_hash: string) => ({ path, blob_hash, size: 1, encoding: 'utf-8' as const });
const expansion = (...entries: ReturnType<typeof entry>[]): ZipExpansion => ({ entries, dirs: [], repositories: [], skipped: [] });
const channels = [{ id: 'c1', name: 'Alpha' }, { id: 'c2', name: 'Beta' }];
/** The page's accounts panel: what opens it, and the revision its close bumps. */
const keys = { onOpenKeys: vi.fn(), accountsRevision: 0 };

const section = (name: string) => screen.getByRole('region', { name });
const createButton = () => screen.getByRole('button', { name: 'Create channel' });
const named = (value = 'New one') => fireEvent.change(screen.getByLabelText('Channel name'), { target: { value } });

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSuggested).mockResolvedValue(catalog());
    vi.mocked(fetchAccounts).mockResolvedValue(accounts([]));
    vi.mocked(fetchChannelOutline).mockResolvedValue({
        capabilities_cells: [{ id: 'g1', name: 'Agent' }, { id: 'r1', name: 'Documents' }],
        main_stacks: [{ id: 's1', name: 'Notes', cells: [{ id: 'n1', name: 'Ideas' }] }, { id: 's2', name: '', cells: [] }],
    });
    vi.mocked(fetchChannelTags).mockResolvedValue([{ tag: 'done', live: 2, removed: 1 }, { tag: 'solo', live: 1, removed: 0 }]);
});
afterEach(() => { cleanup(); });

describe('New channel: the channels page\'s New tab', () => {
    it('opens on Suggested with no channel to copy from, and on Config when channels exist', async () => {
        render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        expect(screen.getByRole('tab', { name: 'Suggested' })).toHaveAttribute('aria-selected', 'true');
        expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Suggested', 'Config']);
        expect(screen.getByLabelText('Channel name')).toBeTruthy();
        await screen.findByText('Agent annotation');
        // Opening reads the catalog once; nothing on the page fetches it again.
        expect(fetchSuggested).toHaveBeenCalledOnce();
        expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
        cleanup();
        vi.mocked(fetchSuggested).mockClear();
        render(<NewTab {...keys} onCreate={vi.fn()} channels={channels} sourceChannelId="c2" />);
        expect(screen.getByRole('tab', { name: 'Config' })).toHaveAttribute('aria-selected', 'true');
        expect(fetchSuggested).not.toHaveBeenCalled();
        expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
    });

    it('shows why Create waits, creates, and shows a failure with Create enabled again', async () => {
        const create = vi.fn().mockRejectedValueOnce(new Error('Name taken')).mockResolvedValue(undefined);
        const cancel = vi.fn();
        render(<NewTab {...keys} onCreate={create} onCancel={cancel} channels={channels} sourceChannelId="c1" />);
        expect(screen.getByText('Name the channel.')).toBeTruthy();
        expect(createButton()).toBeDisabled();
        named('  Gamma ');
        fireEvent.click(createButton());
        expect(await screen.findByRole('alert')).toHaveTextContent('Name taken');
        expect(createButton()).toBeEnabled();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
        expect(create).toHaveBeenLastCalledWith('Gamma', expect.anything());
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(cancel).toHaveBeenCalledOnce();
    });

    it('paints from the skin tokens (S04)', async () => {
        render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        await screen.findByText('Agent annotation');
        expect(screen.getByLabelText('Include explanation cells')).toHaveClass('accent-teal');
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        const painted = [...document.body.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});

describe('New channel: where it goes and when it reads (plan 1105 P7)', () => {
    it('names the default location in one quiet line once the page knows it, and leaves it out until then', async () => {
        const { rerender } = render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        await screen.findByText('Agent annotation');
        expect(document.querySelector('[data-saved-in]')).toBeNull();
        rerender(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} savedIn="/home/me/Collimate/Channels" />);
        expect(document.querySelector('[data-saved-in]')).toHaveTextContent('Saved in /home/me/Collimate/Channels');
    });

    it('reads the Suggested catalog only once the page shows the tab', async () => {
        const { rerender } = render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} shown={false} />);
        await act(async () => {});
        expect(fetchSuggested).not.toHaveBeenCalled();
        rerender(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} shown />);
        await screen.findByText('Agent annotation');
        expect(fetchSuggested).toHaveBeenCalledOnce();
    });
});

describe('New channel: Suggested', () => {
    const open = async (create = vi.fn()) => {
        render(<NewTab {...keys} onCreate={create} channels={[]} sourceChannelId={null} />);
        await screen.findByText('Agent annotation');
        return create;
    };

    it('names a lone suggestion without a picker, describes it, and lists its drafts with their state', async () => {
        await open();
        expect(screen.queryByRole('combobox', { name: 'Suggestion' })).toBeNull();
        expect(screen.getByText('Standard')).toBeTruthy();
        expect(screen.getByText('Every shipped generator and renderer.')).toBeTruthy();
        expect(screen.getByLabelText('Include explanation cells')).toBeChecked();
        expect(screen.getByText('Needs setup: Agent model')).toBeTruthy();
        expect(screen.getByText('Ready')).toBeTruthy();
        expect(screen.getByText("Set up each runner here, or remove the ones you don't need. Edit them later in Capabilities.")).toBeTruthy();
        // Every other runner setting waits for the channel's Capabilities.
        expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
        expect(document.querySelector('[data-runner-editor]')).toBeNull();
    });

    it('picks among several suggestions, resetting the drafts', async () => {
        vi.mocked(fetchSuggested).mockResolvedValue(catalog({ suggestions: [standard, minimal] }));
        await open();
        const picker = screen.getByRole('radiogroup', { name: 'Suggestion' });
        fireEvent.click(within(picker).getByRole('radio', { name: /Minimal/ }));
        expect(within(picker).getByRole('radio', { name: /Minimal/ })).toHaveAttribute('aria-checked', 'true');
        expect(screen.getByText('Web only')).toBeTruthy();
        expect(screen.queryByText('Agent annotation')).toBeNull();
        expect(screen.queryByLabelText('Include explanation cells')).toBeNull();
        expect(screen.getByText('Web only.')).toBeTruthy();
    });

    it('offers a saved suggestion as a card after the app’s, and creates from it with its kept drafts', async () => {
        const mine: SavedSuggestion = {
            ...minimal, id: 'saved-1', name: 'Mine', description: 'My setup.', saved: true, problem: null,
            items: [{ source: 'catalog', id: 'entry-1' }],
        };
        vi.mocked(withSavedSuggestions).mockImplementationOnce(async read => {
            const answer = await read;
            return { ...answer, suggestions: [...answer.suggestions, mine] };
        });
        const create = await open();
        const cards = within(screen.getByRole('radiogroup', { name: 'Suggestion' })).getAllByRole('radio');
        expect(cards.map(card => card.getAttribute('data-suggestion'))).toEqual(['standard', 'saved-1']);
        expect(within(cards[1]).getByText('Saved suggestion')).toBeTruthy();
        expect(within(cards[0]).queryByText('Saved suggestion')).toBeNull();
        fireEvent.click(cards[1]);
        expect(await screen.findByText('Web only')).toBeTruthy();
        named();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        const body = create.mock.calls[0][1] as { saved: { id: string; runners: RunnerDraft[] } };
        expect(body.saved.id).toBe('saved-1');
        expect(body.saved.runners.map(item => item.runner.name)).toEqual(['Web only']);
        expect(Object.keys(body)).toEqual(['saved']);
    });

    it('lists Basic beside Standard as two cards, Standard first and picked, and Basic creates with nothing to set up', async () => {
        const basic: Suggestion = {
            ...standard, id: 'basic', name: 'Basic', description: 'Only the built-ins.',
            generators: [], renderers: [], libraries: [], tutorial: [], runner_generators: [], runners: [],
        };
        vi.mocked(fetchSuggested).mockResolvedValue(catalog({ suggestions: [standard, basic] }));
        const create = await open();
        const cards = within(screen.getByRole('radiogroup', { name: 'Suggestion' })).getAllByRole('radio');
        expect(cards.map(card => card.getAttribute('data-suggestion'))).toEqual(['standard', 'basic']);
        expect(cards.map(card => card.getAttribute('aria-checked'))).toEqual(['true', 'false']);
        expect(within(cards[0]).getByText('Every shipped generator and renderer.')).toBeTruthy();
        expect(within(cards[1]).getByText('Only the built-ins.')).toBeTruthy();
        fireEvent.click(cards[1]);
        expect(cards[1]).toHaveAttribute('aria-checked', 'true');
        expect(screen.queryByText('Agent annotation')).toBeNull();
        expect(screen.queryByLabelText('Include explanation cells')).toBeNull();
        expect(screen.getByText('No runners: add them later in Capabilities.')).toBeTruthy();
        named();
        expect(createButton()).toBeEnabled();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        expect(create.mock.calls[0][1]).toEqual({ suggested: { id: 'basic', tutorial: false, runners: [] } });
    });

    it('gates Create on every remaining draft being ready; Delete confirms, and the body carries the rest', async () => {
        const create = await open();
        named();
        expect(createButton()).toBeDisabled();
        expect(screen.getByText('Configure or remove Agent annotation.')).toBeTruthy();
        const card = screen.getByText('Agent annotation').closest('[data-runner-draft]') as HTMLElement;
        fireEvent.click(within(card).getByRole('button', { name: 'Delete' }));
        expect(screen.getByText('Delete “Agent annotation”?')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
        expect(screen.getByText('Agent annotation')).toBeTruthy();
        fireEvent.click(within(card).getByRole('button', { name: 'Delete' }));
        fireEvent.click(within(card).getAllByRole('button', { name: 'Delete' })[1]);
        expect(screen.queryByText('Agent annotation')).toBeNull();
        fireEvent.click(screen.getByLabelText('Include explanation cells'));
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        const [, body] = create.mock.calls[0];
        expect(body).toEqual({ suggested: { id: 'standard', tutorial: false, runners: [{ runner: expect.objectContaining({ name: 'Web default', generator: 'web' }), files: [{ name: 'notes.txt', content: 'hi' }] }] } });
        // Each draft is offered under an id of its own.
        expect(body.suggested.runners[0].runner.id).toMatch(/^web-default-[0-9a-f]{8}$/);
    });

    it('the accounts buttons open the page\'s accounts panel; its close reads the accounts again, and binding one inline readies the draft', async () => {
        const create = vi.fn();
        const { rerender } = render(<NewTab {...keys} onCreate={create} channels={[]} sourceChannelId={null} />);
        await screen.findByText('Agent annotation');
        named();
        vi.mocked(fetchAccounts).mockResolvedValue(accounts([model]));
        fireEvent.click(screen.getAllByRole('button', { name: 'Open accounts' })[0]);
        expect(keys.onOpenKeys).toHaveBeenLastCalledWith();
        const card = screen.getByText('Agent annotation').closest('[data-runner-draft]') as HTMLElement;
        fireEvent.click(within(card).getByRole('button', { name: 'Open accounts' }));
        expect(keys.onOpenKeys).toHaveBeenCalledTimes(2);
        // No overlay of its own: the panel is the page's.
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(fetchAccounts).toHaveBeenCalledTimes(1);
        rerender(<NewTab {...keys} accountsRevision={1} onCreate={create} channels={[]} sourceChannelId={null} />);
        await waitFor(() => expect(fetchAccounts).toHaveBeenCalledTimes(2));
        // Accounts alone bind nothing; the visible quick field owns this draft's choice.
        expect(screen.getByText('Needs setup: Agent model')).toBeTruthy();
        fireEvent.change(within(card).getByLabelText('Agent model'), { target: { value: 'fast' } });
        await waitFor(() => expect(screen.getAllByText('Ready')).toHaveLength(2));
        expect(createButton()).toBeEnabled();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        expect(create.mock.calls[0][1].suggested.runners[0]).toMatchObject({
            runner: { name: 'Agent annotation', accounts: { model: 'fast' } },
        });
        expect(create.mock.calls[0][1].suggested.runners[0]).not.toHaveProperty('quick_fields');
    });

    it('resolves quick setting conditions from the selected model and requires the revealed fields and prompt', async () => {
        const conditionalAgent: RunnerGenerator = {
            ...agent,
            fields: [
                { key: 'backend', type: 'dropdown', label: 'Agent', default: 'pydantic-ai', account_role: 'model', account_property: 'metadata.harness',
                    options: [{ value: 'pydantic-ai', label: 'Direct model API' }, { value: 'codex', label: 'Codex' }] },
                { key: 'mode', type: 'dropdown', label: 'Mode', default: 'safe', required: true,
                    options: [{ value: 'safe', label: 'Safe' }, { value: 'fast', label: 'Fast', enable_if: { backend: 'codex' } }] },
                { key: 'details', type: 'text', label: 'Details', required: true, show_if: { mode: 'fast' } },
            ],
            prompt_show_if: { mode: 'fast' }, prompt_required_if: { mode: 'fast' },
        };
        const pending = draft('agent-annotation', 'agent', 'Agent annotation');
        pending.runner.prompt = '';
        const suggestion: Suggestion = { ...standard, runner_generators: [conditionalAgent], runners: [offered(pending, [
            { account: 'model' }, { setting: 'mode' }, { setting: 'details' }, { prompt: true },
        ])] };
        vi.mocked(fetchSuggested).mockResolvedValue(catalog({ suggestions: [suggestion] }));
        vi.mocked(fetchAccounts).mockResolvedValue(accounts([{ ...model, metadata: { harness: 'codex' } }]));
        const create = await open();
        named();
        const card = screen.getByText('Agent annotation').closest('[data-runner-draft]') as HTMLElement;
        expect(within(card).getByRole('option', { name: 'Fast' })).toBeDisabled();
        expect(within(card).queryByLabelText('Details')).toBeNull();
        expect(within(card).queryByRole('textbox', { name: /^Prompt/ })).toBeNull();
        fireEvent.change(within(card).getByLabelText('Agent model'), { target: { value: 'fast' } });
        expect(within(card).getByRole('option', { name: 'Fast' })).toBeEnabled();
        fireEvent.change(within(card).getByRole('combobox', { name: /^Mode/ }), { target: { value: 'fast' } });
        expect(within(card).getByRole('textbox', { name: /^Details/ })).toBeTruthy();
        expect(within(card).getByRole('textbox', { name: /^Prompt/ })).toBeTruthy();
        expect(within(card).getByText('Needs setup: Details, Prompt')).toBeTruthy();
        fireEvent.change(within(card).getByRole('textbox', { name: /^Details/ }), { target: { value: 'Use the latest notes' } });
        fireEvent.change(within(card).getByRole('textbox', { name: /^Prompt/ }), { target: { value: 'Review the notes' } });
        expect(createButton()).toBeEnabled();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        expect(create.mock.calls[0][1].suggested.runners).toEqual([expect.objectContaining({ runner: expect.objectContaining({
            accounts: { model: 'fast' }, prompt: 'Review the notes',
            settings: { 'settings.set_yaml': expect.objectContaining({ mode: 'fast', details: 'Use the latest notes' }) },
        }) })]);
    });

    it('keeps quick-field values with their runner through deletion and tab switch, and reads the catalog once', async () => {
        const second = draft('agent-chat', 'agent', 'Agent chat');
        const suggestion: Suggestion = { ...standard, runners: [
            offered(draft('agent-annotation', 'agent', 'Agent annotation'), [{ account: 'model' }]),
            offered(second, [{ account: 'model' }]),
        ] };
        vi.mocked(fetchSuggested).mockResolvedValue(catalog({ suggestions: [suggestion] }));
        vi.mocked(fetchAccounts).mockResolvedValue(accounts([model]));
        const create = await open();
        named();
        const chat = screen.getByText('Agent chat').closest('[data-runner-draft]') as HTMLElement;
        fireEvent.change(within(chat).getByLabelText('Agent model'), { target: { value: 'fast' } });
        const documentCard = screen.getByText('Agent annotation').closest('[data-runner-draft]') as HTMLElement;
        fireEvent.click(within(documentCard).getByRole('button', { name: 'Delete' }));
        fireEvent.click(within(documentCard).getAllByRole('button', { name: 'Delete' })[1]);
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        fireEvent.click(screen.getByRole('tab', { name: 'Suggested' }));
        expect(fetchSuggested).toHaveBeenCalledTimes(1);
        const kept = screen.getByText('Agent chat').closest('[data-runner-draft]') as HTMLElement;
        expect(within(kept).getByLabelText('Agent model')).toHaveValue('fast');
        expect(within(kept).getByText('Ready')).toBeTruthy();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledOnce());
        expect(create.mock.calls[0][1].suggested.runners).toEqual([expect.objectContaining({ runner: expect.objectContaining({
            name: 'Agent chat', accounts: { model: 'fast' },
        }) })]);
    });

    it('says a suggestion without runners leaves them for Capabilities', async () => {
        vi.mocked(fetchSuggested).mockResolvedValue(catalog({ suggestions: [{ ...standard, runners: [] }] }));
        render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        expect(await screen.findByText('No runners: add them later in Capabilities.')).toBeTruthy();
    });

    it('says why the suggestions could not be read, and the Config tab still creates', async () => {
        vi.mocked(fetchSuggested).mockResolvedValueOnce(catalog({ error: 'Cannot read the suggestions in /x: broken', suggestions: [] }));
        render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        expect(await screen.findByText('Could not read the suggestions: Cannot read the suggestions in /x: broken')).toBeTruthy();
        named();
        expect(createButton()).toBeDisabled();
        expect(screen.getByText('No suggestion is available.')).toBeTruthy();
        // The Config tab still creates.
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        expect(createButton()).toBeEnabled();
        cleanup();
        vi.mocked(fetchSuggested).mockRejectedValueOnce(new Error('Request failed (502)'));
        render(<NewTab {...keys} onCreate={vi.fn()} channels={[]} sourceChannelId={null} />);
        expect(await screen.findByText('Could not read the suggestions: Request failed (502)')).toBeTruthy();
    });
});

describe('New channel: Config', () => {
    const open = (create = vi.fn(), list = channels, source: string | null = 'c2') => {
        render(<NewTab {...keys} onCreate={create} channels={list} sourceChannelId={source} />);
        return create;
    };
    const mode = (name: string, choice: string) => fireEvent.click(within(section(name)).getByRole('radio', { name: choice }));

    it('defaults Capabilities to the source channel, main and history cells to none; settings are no part (a new channel starts on the defaults)', async () => {
        const create = open();
        expect(within(section('Capabilities')).getByRole('radio', { name: 'From channel' })).toBeChecked();
        expect(within(section('Capabilities')).getByRole('combobox')).toHaveValue('c2');
        expect(within(section('Main cells')).getByRole('radio', { name: 'None' })).toBeChecked();
        expect(within(section('History cells')).getByRole('radio', { name: 'None' })).toBeChecked();
        expect(screen.queryByRole('region', { name: 'Settings' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Choose JSON…' })).toBeNull();
        expect(screen.getByText(/comes from an existing channel, or from a zip\./)).toBeTruthy();
        expect(fetchChannelOutline).not.toHaveBeenCalled();
        named();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', { config: { packages: { channel_id: 'c2' }, main_cells: null, history_cells: null } }));
    });

    it('with no channels every section is None and From channel is unavailable', () => {
        const create = open(vi.fn(), [], null);
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        for (const name of ['Capabilities', 'Main cells', 'History cells']) {
            expect(within(section(name)).getByRole('radio', { name: 'None' })).toBeChecked();
            expect(within(section(name)).getByRole('radio', { name: 'From channel' })).toBeDisabled();
        }
        named();
        fireEvent.click(createButton());
        expect(create).toHaveBeenCalledWith('New one', { config: { packages: null, main_cells: null, history_cells: null } });
    });

    it('lists a channel’s packages on first expand only, and exports them', async () => {
        open();
        const packages = section('Capabilities');
        const disclosure = within(packages).getByRole('button', { name: 'Capabilities cells' });
        expect(disclosure).toHaveAttribute('aria-expanded', 'false');
        // The built-in cards' additions are the source channel's own.
        expect(within(packages).getByText('Built-in examples and regenerated skills stay in this channel.')).toBeTruthy();
        expect(fetchChannelOutline).not.toHaveBeenCalled();
        fireEvent.click(disclosure);
        expect(disclosure).toHaveAttribute('aria-expanded', 'true');
        expect(await within(packages).findByText('Documents')).toBeTruthy();
        expect(fetchChannelOutline).toHaveBeenCalledWith('c2');
        fireEvent.change(within(packages).getByRole('combobox'), { target: { value: 'c1' } });
        await waitFor(() => expect(fetchChannelOutline).toHaveBeenCalledWith('c1'));
        fireEvent.change(within(packages).getByRole('combobox'), { target: { value: 'c2' } });
        expect(fetchChannelOutline).toHaveBeenCalledTimes(2);
        fireEvent.click(within(packages).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenCalledWith('c2', { scope: 'packages' }, 'Beta packages');
    });

    it('suggests the server\'s download names for the exports, cleaned as the server cleans them', async () => {
        open(vi.fn(), [{ id: 'c3', name: 'My: Channel.' }, { id: 'c4', name: '' }], 'c3');
        fireEvent.click(within(section('Capabilities')).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenLastCalledWith('c3', { scope: 'packages' }, 'My_ Channel packages');
        mode('Main cells', 'From channel');
        fireEvent.click(within(section('Main cells')).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenLastCalledWith('c3', { scope: 'main' }, 'My_ Channel main cells');
        mode('History cells', 'From channel');
        fireEvent.change(await within(section('History cells')).findByRole('combobox', { name: 'Tag' }), { target: { value: 'done' } });
        fireEvent.click(within(section('History cells')).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenLastCalledWith('c3', { scope: 'tag', tag: 'done' }, 'My_ Channel done cells');
        fireEvent.change(within(section('Capabilities')).getByRole('combobox'), { target: { value: 'c4' } });
        fireEvent.click(within(section('Capabilities')).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenLastCalledWith('c4', { scope: 'packages' }, 'Channel packages');
    });

    it('previews a channel’s main stacks and exports them', async () => {
        const create = open();
        mode('Main cells', 'From channel');
        const main = section('Main cells');
        expect(await within(main).findByText('Ideas')).toBeTruthy();
        expect(within(main).getByText('Untitled stack')).toBeTruthy();
        fireEvent.click(within(main).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenCalledWith('c2', { scope: 'main' }, 'Beta main cells');
        named();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', expect.objectContaining({ config: expect.objectContaining({ main_cells: { channel_id: 'c2' } }) })));
    });

    it('picks a tag set with its cell count, which Create needs, and exports it', async () => {
        const create = open();
        mode('History cells', 'From channel');
        const history = section('History cells');
        const tag = await within(history).findByRole('combobox', { name: 'Tag' });
        expect(within(history).getByRole('option', { name: 'done · 3 cells' })).toBeTruthy();
        expect(within(history).getByRole('option', { name: 'solo · 1 cell' })).toBeTruthy();
        named();
        expect(createButton()).toBeDisabled();
        expect(screen.getByText('Choose a tag for History cells.')).toBeTruthy();
        expect(within(history).getByRole('button', { name: 'Export zip' })).toBeDisabled();
        fireEvent.change(tag, { target: { value: 'done' } });
        fireEvent.click(within(history).getByRole('button', { name: 'Export zip' }));
        expect(exportZip).toHaveBeenCalledWith('c2', { scope: 'tag', tag: 'done' }, 'Beta done cells');
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', expect.objectContaining({ config: expect.objectContaining({ history_cells: { channel_id: 'c2', tag: 'done' } }) })));
    });

    it('says when a channel has no tags', async () => {
        vi.mocked(fetchChannelTags).mockResolvedValue([]);
        open();
        mode('History cells', 'From channel');
        expect(await within(section('History cells')).findByText('No tags')).toBeTruthy();
    });

    it('reads a packages zip from the picker, and refuses one with other folders', async () => {
        const create = open();
        mode('Capabilities', 'From file');
        named();
        expect(screen.getByText('Choose a zip for Capabilities.')).toBeTruthy();
        const zip = new File(['PK'], 'beta.zip', { type: 'application/zip' });
        vi.mocked(pickZip).mockResolvedValue(zip);
        vi.mocked(expandZipForNewChannel).mockResolvedValueOnce(expansion(entry('Beta/Capabilities/Agent/agent.generator', 'h1'), entry('Beta/Notes/Ideas/a.md', 'h2')));
        const packages = section('Capabilities');
        fireEvent.click(within(packages).getByRole('button', { name: 'Choose zip…' }));
        expect(await within(packages).findByText(/also holds Notes/)).toBeTruthy();
        expect(createButton()).toBeDisabled();
        vi.mocked(expandZipForNewChannel).mockResolvedValueOnce(expansion(entry('Beta/Capabilities/Agent/agent.generator', 'h1'), entry('Beta/Capabilities/Docs/a.handler', 'h2')));
        fireEvent.click(within(packages).getByRole('button', { name: 'Choose zip…' }));
        expect(await within(packages).findByText('2 Capabilities cells', { exact: false })).toBeTruthy();
        expect(expandZipForNewChannel).toHaveBeenCalledWith(zip, { signal: expect.any(AbortSignal) });
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', expect.objectContaining({ config: expect.objectContaining({ packages: { cells: [
            { name: 'Agent', files: [{ path: 'agent.generator', blob_hash: 'h1' }], dirs: [] },
            { name: 'Docs', files: [{ path: 'a.handler', blob_hash: 'h2' }], dirs: [] },
        ] } }) })));
    });

    it('takes a main zip dropped on the section, and refuses a drop that is no zip', async () => {
        const create = open();
        mode('Main cells', 'From file');
        const zone = screen.getByTestId('Main cells drop zone');
        fireEvent.drop(zone, { dataTransfer: { types: ['Files'], files: [new File(['x'], 'notes.txt')], dropEffect: 'none' } });
        expect(await within(section('Main cells')).findByText('notes.txt: Not a zip file.')).toBeTruthy();
        vi.mocked(expandZipForNewChannel).mockResolvedValueOnce(expansion(entry('Beta/Notes/Ideas/a.md', 'h1'), entry('Beta/Notes/Plans/b.md', 'h2')));
        fireEvent.drop(zone, { dataTransfer: { types: ['Files'], files: [new File(['PK'], 'main.zip')], dropEffect: 'none' } });
        expect(await within(section('Main cells')).findByText('1 stack, 2 cells', { exact: false })).toBeTruthy();
        named();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', expect.objectContaining({ config: expect.objectContaining({ main_cells: { stacks: [{ name: 'Notes', cells: [
            { name: 'Ideas', files: [{ path: 'a.md', blob_hash: 'h1' }], dirs: [] },
            { name: 'Plans', files: [{ path: 'b.md', blob_hash: 'h2' }], dirs: [] },
        ] }] } }) })));
    });

    it('reads a tag zip with its tag prefilled from the root folder, editable', async () => {
        const create = open();
        mode('History cells', 'From file');
        vi.mocked(pickZip).mockResolvedValue(new File(['PK'], 'done.zip'));
        vi.mocked(expandZipForNewChannel).mockResolvedValueOnce(expansion(entry('done/Ideas/a.md', 'h1')));
        fireEvent.click(within(section('History cells')).getByRole('button', { name: 'Choose zip…' }));
        const tag = await within(section('History cells')).findByRole('textbox', { name: 'Tag' });
        expect(tag).toHaveValue('done');
        fireEvent.change(tag, { target: { value: 'Done & dusted' } });
        named();
        fireEvent.click(createButton());
        await waitFor(() => expect(create).toHaveBeenCalledWith('New one', expect.objectContaining({ config: expect.objectContaining({
            history_cells: { tag: 'Done & dusted', cells: [{ name: 'Ideas', files: [{ path: 'a.md', blob_hash: 'h1' }], dirs: [] }] },
        }) })));
    });

    it('keeps the Config tab\'s fetches under way across a tab switch', async () => {
        let answer!: (value: Awaited<ReturnType<typeof fetchChannelOutline>>) => void;
        vi.mocked(fetchChannelOutline).mockReturnValueOnce(new Promise(resolve => { answer = resolve; }));
        open();
        mode('Main cells', 'From channel');
        await waitFor(() => expect(fetchChannelOutline).toHaveBeenCalledWith('c2'));
        fireEvent.click(screen.getByRole('tab', { name: 'Suggested' }));
        fireEvent.click(screen.getByRole('tab', { name: 'Config' }));
        await act(async () => { answer({ capabilities_cells: [], main_stacks: [{ id: 's1', name: 'Notes', cells: [{ id: 'n1', name: 'Ideas' }] }] }); });
        expect(within(section('Main cells')).getByText('Ideas')).toBeTruthy();
        expect(fetchChannelOutline).toHaveBeenCalledTimes(1);
    });

    it('cancels a zip upload when the section takes another file, leaves From file, or the page closes', async () => {
        const signals: AbortSignal[] = [];
        vi.mocked(expandZipForNewChannel).mockImplementation((_zip, options) => new Promise((_resolve, reject) => {
            const signal = options!.signal!;
            signals.push(signal);
            signal.addEventListener('abort', () => reject(new DOMException('The upload was cancelled.', 'AbortError')));
        }));
        const { unmount } = render(<NewTab {...keys} onCreate={vi.fn()} onCancel={vi.fn()} channels={channels} sourceChannelId="c2" />);
        mode('Main cells', 'From file');
        const main = section('Main cells');
        vi.mocked(pickZip).mockResolvedValueOnce(new File(['PK'], 'first.zip')).mockResolvedValueOnce(new File(['PK'], 'second.zip'));
        fireEvent.click(within(main).getByRole('button', { name: 'Choose zip…' }));
        expect(await within(main).findByText('Uploading first.zip…')).toBeTruthy();
        fireEvent.click(within(main).getByRole('button', { name: 'Choose zip…' }));
        expect(await within(main).findByText('Uploading second.zip…')).toBeTruthy();
        expect(signals.map(signal => signal.aborted)).toEqual([true, false]);
        mode('Main cells', 'None');
        expect(signals[1].aborted).toBe(true);
        mode('Main cells', 'From file');
        expect(await within(section('Main cells')).findByText('second.zip: The upload was cancelled.')).toBeTruthy();
        named();
        expect(screen.getByText('Choose a zip for Main cells.')).toBeTruthy();
        mode('History cells', 'From file');
        vi.mocked(pickZip).mockResolvedValueOnce(new File(['PK'], 'done.zip'));
        fireEvent.click(within(section('History cells')).getByRole('button', { name: 'Choose zip…' }));
        expect(await within(section('History cells')).findByText('Uploading done.zip…')).toBeTruthy();
        unmount();
        expect(signals[2].aborted).toBe(true);
    });

    it('waits for an upload before Create', async () => {
        open();
        mode('Main cells', 'From file');
        vi.mocked(pickZip).mockResolvedValue(new File(['PK'], 'main.zip'));
        vi.mocked(expandZipForNewChannel).mockReturnValueOnce(new Promise(() => {}));
        fireEvent.click(within(section('Main cells')).getByRole('button', { name: 'Choose zip…' }));
        expect(await screen.findByText('Uploading main.zip…')).toBeTruthy();
        named();
        expect(createButton()).toBeDisabled();
        expect(screen.getByText('Main cells: main.zip is still uploading.')).toBeTruthy();
    });
});
