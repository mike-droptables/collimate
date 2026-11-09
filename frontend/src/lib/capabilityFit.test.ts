import { describe, expect, it } from 'vitest';
import type { GeneratorEntry, RendererEntry } from './api/catalog';
import type { HandlerInfo } from './definitionApi';
import type { RunnerGenerator } from './runnerApi';
import {
    assessFit, fitCatalogs, fitText, knownTypes, normalizeExt, readyToDescribe, stepAnswers, type FitAnswers, type FitCatalogs,
} from './capabilityFit';

const catalogs: FitCatalogs = {
    generators: [
        { id: 'synth-gen', name: 'Synth generator', summary: 'Makes synthetic files.' },
        { id: 'prompted', name: 'Prompted', summary: 'Follows written instructions.' },
    ],
    promptGenerators: ['prompted'],
    renderers: [
        { id: 'memo-view', name: 'Memo view', extensions: ['.memo', '.md'], cellName: 'Views' },
        { id: 'table', name: 'Table', extensions: ['.csv'] },
    ],
    handlers: [{ id: 'memo', name: 'Memo', cellId: 'views', folder: 'memo', cellName: 'Views', types: [{ ext: '.memo' }] }],
};
const assess = (answers: FitAnswers, from = catalogs) => assessFit(answers, from);

describe('normalizeExt', () => {
    it('lowercases and keeps one leading dot', () => {
        expect(normalizeExt('Recipe')).toBe('.recipe');
        expect(normalizeExt(' .CSV ')).toBe('.csv');
        expect(normalizeExt('*.md')).toBe('.md');
        expect(normalizeExt('..x')).toBe('.x');
        expect(normalizeExt('')).toBe('');
        expect(normalizeExt(undefined)).toBe('');
    });

    it('knownTypes merges renderer extensions and handler types, sorted and unique', () => {
        expect(knownTypes(catalogs)).toEqual(['.csv', '.md', '.memo']);
    });
});

describe('a. Understand a new kind of file', () => {
    it('a type a handler already claims ends the questionnaire: a second handler would clash', () => {
        const result = assess({ path: 'file-type', ext: 'MEMO' });
        expect(result.fits).toEqual([{ kind: 'handled', ext: '.memo', handler: catalogs.handlers[0], ends: true }]);
        expect(fitText(result.fits[0])).toBe('‘Memo’ already handles .memo files (in Views).');
        expect(result.make).toEqual([]);
        expect(readyToDescribe({ path: 'file-type', ext: 'memo' }, result)).toBe(false);
        expect(result.related).toEqual([{ kind: 'handler', id: 'memo', name: 'Memo' }]);
    });

    it('a new type asks how it looks: like an existing type, a new view or plain text', () => {
        expect(assess({ path: 'file-type', ext: '.recipe' }).make).toEqual([]);
        expect(assess({ path: 'file-type', ext: '.recipe', look: 'existing' }).make).toEqual([]);
        const like = assess({ path: 'file-type', ext: '.recipe', look: 'existing', lookAs: '.md' });
        expect(like.make).toEqual(['handler']);
        expect(like.related).toEqual([{ kind: 'renderer', id: 'memo-view', name: 'Memo view' }]);
        expect(assess({ path: 'file-type', ext: '.recipe', look: 'new-view' }).make).toEqual(['handler', 'renderer']);
        const plain = assess({ path: 'file-type', ext: '.recipe', look: 'plain' });
        expect(plain.make).toEqual(['handler']);
        expect(readyToDescribe({ path: 'file-type', ext: '.recipe', look: 'plain' }, plain)).toBe(true);
    });
});

describe('b. Show files a new way', () => {
    it('a renderer that lists the type is a fit; continuing needs ‘I still need a new view’', () => {
        const answers: FitAnswers = { path: 'show', ext: 'csv' };
        const result = assess(answers);
        expect(result.fits).toEqual([{ kind: 'shown', ext: '.csv', renderer: catalogs.renderers[1], ends: false }]);
        expect(fitText(result.fits[0])).toBe('‘Table’ already shows .csv files.');
        expect(result.make).toEqual([]);
        expect(readyToDescribe(answers, result)).toBe(false);
        expect(assess({ ...answers, newView: true }).make).toEqual(['renderer']);
    });

    it('a type nothing shows calls for a renderer', () => {
        expect(assess({ path: 'show', ext: '.tsv' })).toEqual({ fits: [], make: ['renderer'], related: [] });
    });
});

describe('c. A variant of a type it already shows', () => {
    it('its own extension over a shown base reuses the renderer through a handler', () => {
        const result = assess({ path: 'variant', base: '.md', ownExt: 'notes' });
        expect(result.fits.map(fit => fit.kind)).toEqual(['reuse']);
        expect(fitText(result.fits[0])).toBe('A handler for .notes with render_as .md reuses ‘Memo view’.');
        expect(result.make).toEqual(['handler']);
    });

    it('no extension and no checks: nothing new is needed, and that ends it', () => {
        const result = assess({ path: 'variant', base: '.md', checks: false });
        expect(result.fits).toEqual([{ kind: 'nothing-new', renderer: catalogs.renderers[0], ends: true }]);
        expect(fitText(result.fits[0])).toBe('Nothing new is needed: ‘Memo view’ already shows it.');
        expect(result.make).toEqual([]);
    });

    it('no extension, checks wanted, a handler claims the base: change that handler instead', () => {
        const result = assess({ path: 'variant', base: '.memo', checks: true });
        expect(result.fits).toEqual([{ kind: 'change-handler', handler: catalogs.handlers[0], ends: true }]);
        expect(fitText(result.fits[0])).toBe('Change ‘Memo’’s guidance and examples.');
    });

    it('checks wanted on a base no handler claims calls for a handler; waits for the checks answer', () => {
        expect(assess({ path: 'variant', base: '.md', checks: true }).make).toEqual(['handler']);
        expect(assess({ path: 'variant', base: '.md' }).make).toEqual([]);
        expect(assess({ path: 'variant' }).make).toEqual([]);
        expect(assess({ path: 'variant', base: '.tsv', checks: false }).make).toEqual(['renderer']);
    });
});

describe('d. Run a program I already have', () => {
    it('a generator that can run it: a runner is enough, and that ends it', () => {
        const result = assess({ path: 'program', generator: 'synth-gen' });
        expect(result.fits).toEqual([{ kind: 'runner-enough', generator: catalogs.generators[0], ends: true }]);
        expect(fitText(result.fits[0])).toBe('A runner is enough: it saves the program and settings for ‘Synth generator’.');
        expect(result.make).toEqual([]);
    });

    it('None of these calls for a generator and a runner; nothing picked yet calls for nothing', () => {
        expect(assess({ path: 'program', generator: null }).make).toEqual(['generator', 'runner']);
        expect(assess({ path: 'program' }).make).toEqual([]);
    });
});

describe('e. A new way of making files', () => {
    it('fixed steps call for a generator and a runner', () => {
        expect(assess({ path: 'making', how: 'fixed' }).make).toEqual(['generator', 'runner']);
    });

    it('written instructions list the generators that take a prompt, until None fit', () => {
        const result = assess({ path: 'making', how: 'flexible' });
        expect(result.fits).toEqual([{ kind: 'runner-of', generators: [catalogs.generators[1]], ends: false }]);
        expect(fitText(result.fits[0])).toBe('A runner of an existing generator, given written instructions, may be enough:');
        expect(result.make).toEqual([]);
        expect(assess({ path: 'making', how: 'flexible', noneFit: true }).make).toEqual(['generator', 'runner']);
        expect(assess({ path: 'making', how: 'flexible' }, { ...catalogs, promptGenerators: [] }).make).toEqual(['generator', 'runner']);
        expect(assess({ path: 'making' }).make).toEqual([]);
    });
});

describe('f. Not sure', () => {
    it('lets the work stack decide: nothing to make, ready to describe', () => {
        const result = assess({ path: 'unsure' });
        expect(result).toEqual({ fits: [], make: [], related: [] });
        expect(readyToDescribe({ path: 'unsure' }, result)).toBe(true);
        expect(readyToDescribe({ path: null }, assess({ path: null }))).toBe(false);
    });
});

describe('the answers a brief records', () => {
    it('lists the steps’ questions in order, with what was answered', () => {
        expect(stepAnswers({ path: 'file-type', ext: 'Recipe', look: 'existing', lookAs: '.md' }, catalogs)).toEqual([
            { question: 'What should this channel learn to do?', answer: 'Understand a new kind of file' },
            { question: 'File extension', answer: '.recipe' },
            { question: 'How should these files look?', answer: 'Like an existing type' },
            { question: 'Shown like', answer: '.md' },
        ]);
        expect(stepAnswers({ path: 'program', generator: 'synth-gen' }, catalogs).at(-1)).toEqual({ question: 'Can one of these run it?', answer: 'Synth generator' });
        expect(stepAnswers({ path: 'program', generator: null }, catalogs).at(-1)).toEqual({ question: 'Can one of these run it?', answer: 'None of these' });
        expect(stepAnswers({ path: 'variant', base: '.md', checks: false }, catalogs).map(line => line.answer)).toEqual(['A variant of a type it already shows', '.md', 'No']);
        expect(stepAnswers({ path: 'making', how: 'flexible', noneFit: true }, catalogs).map(line => line.answer))
            .toEqual(['A new way of making files', 'Flexibly, by following written instructions', 'None fit']);
        expect(stepAnswers({ path: 'show', ext: '.csv', newView: true }, catalogs).map(line => line.answer)).toEqual(['Show files a new way', '.csv', 'I still need a new view']);
        expect(stepAnswers({ path: 'unsure' }, catalogs)).toEqual([{ question: 'What should this channel learn to do?', answer: 'Not sure: let me describe it' }]);
        expect(stepAnswers({ path: null }, catalogs)).toEqual([]);
    });
});

describe('fitCatalogs', () => {
    it('reads the manage view’s catalogs: live renderers and handlers, and the generators whose runners take a prompt', () => {
        const generators = [{ name: 'synth-gen', display_name: 'Synth generator', summary: 'Makes files.' }] as GeneratorEntry[];
        const runnerGenerators = [{ id: 'synth-gen', prompt_intent: 'The task' }, { id: 'quiet' }] as RunnerGenerator[];
        const renderers = [
            { id: 'memo-view', name: 'Memo view', extensions: ['.memo'], cell_id: 'views', available: true },
            { id: 'off', name: 'Off', extensions: ['.off'], available: false },
        ] as RendererEntry[];
        const handlers = [
            { id: 'memo', name: 'Memo', cell_id: 'views', folder: 'memo', types: [{ ext: '.memo' }], disabled: false },
            { id: 'memo', name: 'Twin', cell_id: 'other', folder: 'twin', types: [{ ext: '.memo' }], disabled: true },
        ] as HandlerInfo[];
        expect(fitCatalogs({ generators, runnerGenerators, renderers, handlers }, id => (id === 'views' ? 'Views' : ''))).toEqual({
            generators: [{ id: 'synth-gen', name: 'Synth generator', summary: 'Makes files.' }],
            promptGenerators: ['synth-gen'],
            renderers: [{ id: 'memo-view', name: 'Memo view', extensions: ['.memo'], cellName: 'Views' }],
            handlers: [{ id: 'memo', name: 'Memo', cellId: 'views', folder: 'memo', cellName: 'Views', types: [{ ext: '.memo' }] }],
        });
        expect(fitCatalogs({}, () => '')).toEqual({ generators: [], promptGenerators: [], renderers: [], handlers: [] });
    });
});
