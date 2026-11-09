import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import CreateQuestionnaire from '../CreateQuestionnaire';
import { RENDERER_PREFS_CHANGED_EVENT, saveRendererPreferences } from '@/lib/api';
import { spawnCapabilityCreate, type SpawnResult } from '@/lib/api/capabilitiesStack';
import { openSpawnedCell } from '@/lib/capabilitySpawn';
import type { FitCatalogs } from '@/lib/capabilityFit';
import { seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({ ...(await original<Record<string, unknown>>()), saveRendererPreferences: vi.fn(async () => ({})) }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), spawnCapabilityCreate: vi.fn() }));
vi.mock('@/lib/capabilitySpawn', () => ({ openSpawnedCell: vi.fn() }));

const catalogs: FitCatalogs = {
    generators: [
        { id: 'synth-gen', name: 'Synth generator', summary: 'Makes synthetic files.' },
        { id: 'prompted', name: 'Prompted', summary: 'Follows written instructions.' },
    ],
    promptGenerators: ['prompted'],
    renderers: [{ id: 'memo-view', name: 'Memo view', extensions: ['.memo', '.md'], cellName: 'Views' }, { id: 'table', name: 'Table', extensions: ['.csv'] }],
    handlers: [{ id: 'memo', name: 'Memo', cellId: 'views', folder: 'memo', cellName: 'Views', types: [{ ext: '.memo' }] }],
};
const SPAWN = { stack_id: 'create-stack', cell_id: 'create-cell', brief_path: 'brief.md' } as SpawnResult;
const onExit = vi.fn();
const onCancel = vi.fn();

function mount(initialDescription?: string) {
    render(<CreateQuestionnaire catalogs={catalogs} prefs={{ '.md': 'memo-view' }} initialDescription={initialDescription} onCancel={onCancel} onExit={onExit} />);
}
const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const choose = (label: RegExp | string) => fireEvent.click(screen.getByRole('radio', { name: label }));
const type = (label: string, value: string) => fireEvent.change(screen.getByRole('textbox', { name: label }), { target: { value } });
const fit = () => screen.getByRole('status');

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    vi.mocked(spawnCapabilityCreate).mockResolvedValue(SPAWN);
});
afterEach(cleanup);

describe('Create a capability', () => {
    it('steps through four steps; Next waits for each answer', () => {
        mount();
        expect(screen.getByRole('list', { name: 'Steps' })).toHaveTextContent('1. Choose2. Details3. Describe4. Confirm');
        expect(within(screen.getByRole('list', { name: 'Steps' })).getAllByRole('listitem')[0]).toHaveAttribute('aria-current', 'step');
        const labels = within(screen.getByRole('group', { name: 'What should this channel learn to do?' })).getAllByRole('radio').map(radio => radio.closest('label')!.textContent);
        expect(labels).toEqual([
            'Understand a new kind of filee.g. ‘a .recipe file my agents write and the app checks’',
            'Show files a new waye.g. ‘.csv files as a sortable table’',
            'A variant of a type it already showse.g. ‘meeting notes: Markdown with fixed headings’',
            'Run a program I already havee.g. ‘run ./export.sh or my CLI tool with its own settings’',
            'A new way of making filese.g. ‘turn a spreadsheet into a weekly report’',
            'Not sure: let me describe it',
        ]);
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        choose(/^Understand a new kind of file/);
        next();
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(screen.getByRole('radio', { name: /^Understand a new kind of file/ })).toBeChecked();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onCancel).toHaveBeenCalled();
    });

    it('a. a type a handler already claims ends with Show it or Add an example, never a spawn', () => {
        mount();
        choose(/^Understand a new kind of file/); next();
        type('File extension', 'MEMO');
        expect(fit()).toHaveTextContent('‘Memo’ already handles .memo files (in Views).');
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.click(within(fit()).getByRole('button', { name: 'Add an example' }));
        expect(onExit).toHaveBeenCalledWith({ kind: 'handler', cellId: 'views', folder: 'memo', step: 'addExample' });
        fireEvent.click(within(fit()).getByRole('button', { name: 'Show it' }));
        expect(onExit).toHaveBeenLastCalledWith({ kind: 'handler', cellId: 'views', folder: 'memo' });
        expect(spawnCapabilityCreate).not.toHaveBeenCalled();
    });

    it('b. a renderer that shows it can become the default, which ends it without a spawn', async () => {
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            mount();
            choose(/^Show files a new way/); next();
            type('File extension', '.csv');
            expect(fit()).toHaveTextContent('‘Table’ already shows .csv files.');
            expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
            fireEvent.click(within(fit()).getByRole('button', { name: 'Make it the default' }));
            await waitFor(() => expect(onExit).toHaveBeenCalledWith({ kind: 'notice', text: '‘Table’ is now the default for .csv files.' }));
            expect(saveRendererPreferences).toHaveBeenCalledWith({ '.md': 'memo-view', '.csv': 'table' });
            expect(announced).toHaveBeenCalled();
            expect(spawnCapabilityCreate).not.toHaveBeenCalled();
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('b. ‘I still need a new view’ carries on; Yes to a variant switches to path c', () => {
        mount();
        choose(/^Show files a new way/); next();
        type('File extension', '.csv');
        fireEvent.click(screen.getByRole('button', { name: 'I still need a new view' }));
        expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Yes' }));
        expect(screen.getByRole('region', { name: 'A variant of a type it already shows' })).toBeInTheDocument();
        expect(screen.getByRole('textbox', { name: 'Its own extension' })).toHaveValue('.csv');
    });

    it('c. no own extension, no checks: nothing new is needed', () => {
        mount();
        choose(/^A variant/); next();
        fireEvent.change(screen.getByRole('combobox', { name: 'Based on which type?' }), { target: { value: '.md' } });
        choose('No');
        expect(fit()).toHaveTextContent('Nothing new is needed: ‘Memo view’ already shows it.');
        fireEvent.click(within(fit()).getByRole('button', { name: 'Done' }));
        expect(onCancel).toHaveBeenCalled();
        expect(spawnCapabilityCreate).not.toHaveBeenCalled();
    });

    it('c. checks on a type a handler claims: change that handler instead', () => {
        mount();
        choose(/^A variant/); next();
        fireEvent.change(screen.getByRole('combobox', { name: 'Based on which type?' }), { target: { value: '.memo' } });
        choose('Yes');
        expect(fit()).toHaveTextContent('Change ‘Memo’’s guidance and examples.');
        fireEvent.click(within(fit()).getByRole('button', { name: 'Show it' }));
        expect(onExit).toHaveBeenCalledWith({ kind: 'handler', cellId: 'views', folder: 'memo' });
    });

    it('d. a generator that runs it: a runner is enough, with the description carried over', () => {
        mount('nightly export');
        choose(/^Run a program/); next();
        choose(/^Synth generator/);
        expect(fit()).toHaveTextContent('A runner is enough: it saves the program and settings for ‘Synth generator’.');
        fireEvent.click(within(fit()).getByRole('button', { name: 'Make a runner' }));
        expect(onExit).toHaveBeenCalledWith({ kind: 'runner', generatorId: 'synth-gen', description: 'nightly export' });
        expect(spawnCapabilityCreate).not.toHaveBeenCalled();
    });

    it('e. written instructions offer a runner of a prompted generator, until None fit', () => {
        mount();
        choose(/^A new way of making files/); next();
        choose('Flexibly, by following written instructions');
        expect(fit()).toHaveTextContent('A runner of an existing generator, given written instructions, may be enough:');
        expect(within(fit()).queryByText('Synth generator')).toBeNull();
        fireEvent.click(within(fit()).getByRole('button', { name: 'Make a runner for it' }));
        expect(onExit).toHaveBeenCalledWith({ kind: 'runner', generatorId: 'prompted', description: '' });
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        fireEvent.click(within(fit()).getByRole('button', { name: 'None fit' }));
        expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
    });

    it('confirm spawns the work stack with every answer shown, the kinds and what is related', async () => {
        mount();
        choose(/^Understand a new kind of file/); next();
        type('File extension', 'Recipe');
        choose('Like an existing type');
        fireEvent.change(screen.getByRole('combobox', { name: 'Shown like' }), { target: { value: '.md' } });
        next();
        expect(screen.getByRole('textbox', { name: 'What should it do?' })).toHaveAttribute('placeholder', 'a .recipe file my agents write and the app checks');
        expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
        type('Name', 'Recipe');
        type('What should it do?', 'Recipes my agents write');
        type('Anything it must follow?', 'a title, ingredients and steps');
        next();
        const confirm = screen.getByRole('region', { name: 'Confirm' });
        expect(within(confirm).getByRole('checkbox', { name: /^A handler/ })).toBeChecked();
        expect(within(confirm).getByRole('checkbox', { name: /^A renderer/ })).not.toBeChecked();
        expect(confirm).toHaveTextContent('Related in this channel: Memo view');
        expect(confirm).toHaveTextContent('A new stack opens beside Capabilities with a brief of your answers and the authoring guides. Run any runner there. A runner that starts from the open file follows brief.md; with any other, tell it to follow brief.md. When its files are ready, add each one from that stack’s Files panel — a generator before its runners. A new handler then needs a Build from its card.');
        fireEvent.click(within(confirm).getByRole('checkbox', { name: /^A renderer/ }));
        fireEvent.click(screen.getByRole('button', { name: 'Open work stack' }));
        await waitFor(() => expect(openSpawnedCell).toHaveBeenCalledWith(SPAWN));
        expect(spawnCapabilityCreate).toHaveBeenCalledWith('ch', {
            title: 'Recipe',
            make: ['renderer', 'handler'],
            answers: [
                { question: 'What should this channel learn to do?', answer: 'Understand a new kind of file' },
                { question: 'File extension', answer: '.recipe' },
                { question: 'How should these files look?', answer: 'Like an existing type' },
                { question: 'Shown like', answer: '.md' },
                { question: 'Name', answer: 'Recipe' },
                { question: 'What should it do?', answer: 'Recipes my agents write' },
                { question: 'Anything it must follow?', answer: 'a title, ingredients and steps' },
            ],
            related: [{ kind: 'renderer', id: 'memo-view' }],
        });
        expect(onExit).toHaveBeenCalledWith({ kind: 'notice', text: 'Work stack ‘Create Recipe’ is open. Run any runner there. A runner that starts from the open file follows brief.md; with any other, tell it to follow brief.md. Then add what it makes from its Files panel.' });
    });

    it('Not sure lets the work stack decide: nothing to make', async () => {
        mount();
        choose(/^Not sure/); next();
        expect(screen.getByText(/^Let the work stack decide/)).toBeInTheDocument();
        next();
        type('Name', 'Mystery');
        type('What should it do?', 'Something new');
        next();
        expect(screen.getByText('Nothing chosen: the work stack decides what to make.')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Mystery');
        next();
        fireEvent.click(screen.getByRole('button', { name: 'Open work stack' }));
        await waitFor(() => expect(spawnCapabilityCreate).toHaveBeenCalledWith('ch', expect.objectContaining({ title: 'Mystery', make: [], related: [] })));
    });

    it('a failed spawn stays on the confirm step with the reason', async () => {
        vi.mocked(spawnCapabilityCreate).mockRejectedValueOnce(new Error('Server busy'));
        mount();
        choose(/^Not sure/); next(); next();
        type('Name', 'Mystery');
        type('What should it do?', 'Something new');
        next();
        fireEvent.click(screen.getByRole('button', { name: 'Open work stack' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('Server busy');
        expect(openSpawnedCell).not.toHaveBeenCalled();
        expect(onExit).not.toHaveBeenCalled();
    });
});
