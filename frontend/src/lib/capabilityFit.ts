import type { GeneratorEntry, RendererEntry } from './api/catalog';
import type { HandlerInfo } from './definitionApi';
import type { RunnerGenerator } from './runnerApi';
import type { CapabilityKind } from './types';

/**
 * The Create questionnaire's fit check: from the answers so far and what the
 * channel already holds, whether something there already does the job (a
 * fit), and which kinds a work stack should make. It reads only the
 * channel's own catalogs, never a shipped name.
 */

/** Step 1's choice. */
export type FitPath = 'file-type' | 'show' | 'variant' | 'program' | 'making' | 'unsure';

/** Step 1's choices, each with its example (step 3's placeholder too). */
export const FIT_PATHS: readonly { path: FitPath; label: string; example: string }[] = [
    { path: 'file-type', label: 'Understand a new kind of file', example: 'a .recipe file my agents write and the app checks' },
    { path: 'show', label: 'Show files a new way', example: '.csv files as a sortable table' },
    { path: 'variant', label: 'A variant of a type it already shows', example: 'meeting notes: Markdown with fixed headings' },
    { path: 'program', label: 'Run a program I already have', example: 'run ./export.sh or my CLI tool with its own settings' },
    { path: 'making', label: 'A new way of making files', example: 'turn a spreadsheet into a weekly report' },
    { path: 'unsure', label: 'Not sure: let me describe it', example: 'what it should do, in your own words' },
];

/** The questions as the questionnaire asks them (and as the brief records
 *  them). */
export const FIT_QUESTIONS = {
    path: 'What should this channel learn to do?',
    ext: 'File extension',
    look: 'How should these files look?',
    lookAs: 'Shown like',
    variant: 'Is it a variant of a type the channel knows?',
    base: 'Based on which type?',
    ownExt: 'Its own extension',
    checks: 'Must its files pass checks?',
    generator: 'Can one of these run it?',
    how: 'How should it work?',
    name: 'Name',
    what: 'What should it do?',
    follow: 'Anything it must follow?',
} as const;

export const LOOK_LABELS = { existing: 'Like an existing type', 'new-view': 'A new view', plain: 'As plain text' } as const;
export const HOW_LABELS = { fixed: 'The same fixed steps every time (a program)', flexible: 'Flexibly, by following written instructions' } as const;

export interface FitAnswers {
    path: FitPath | null;
    /** Paths a and b: the extension, as typed. */
    ext?: string;
    /** Path a. */
    look?: keyof typeof LOOK_LABELS;
    /** Path a, 'Like an existing type': that type's extension. */
    lookAs?: string;
    /** Path b: 'I still need a new view' over a renderer that shows it. */
    newView?: boolean;
    /** Path c. */
    base?: string;
    ownExt?: string;
    checks?: boolean;
    /** Path d: a generator's id; null is 'None of these'. */
    generator?: string | null;
    /** Path e. */
    how?: keyof typeof HOW_LABELS;
    /** Path e, flexible: 'None fit'. */
    noneFit?: boolean;
}

export interface FitGenerator { id: string; name: string; summary?: string }
export interface FitRenderer { id: string; name: string; extensions: readonly string[]; cellName?: string }
export interface FitHandler { id: string; name: string; cellId: string; folder: string; cellName?: string; types: readonly { ext: string }[] }

/** The channel's catalogs as the fit check reads them. */
export interface FitCatalogs {
    generators: readonly FitGenerator[];
    /** The generators whose runners take a prompt (written instructions). */
    promptGenerators: readonly string[];
    renderers: readonly FitRenderer[];
    handlers: readonly FitHandler[];
}

/** The manage view's catalogs as the fit check reads them; `cellName`
 *  names a holding cell. */
export function fitCatalogs(data: {
    generators?: readonly GeneratorEntry[];
    runnerGenerators?: readonly RunnerGenerator[];
    renderers?: readonly RendererEntry[];
    handlers?: readonly HandlerInfo[];
}, cellName: (cellId: string | undefined) => string): FitCatalogs {
    return {
        generators: (data.generators ?? []).map(generator => ({ id: generator.name, name: generator.display_name, summary: generator.summary })),
        promptGenerators: (data.runnerGenerators ?? []).filter(generator => !!generator.prompt_intent).map(generator => generator.id),
        renderers: (data.renderers ?? []).filter(renderer => renderer.available !== false)
            .map(renderer => ({ id: renderer.id, name: renderer.name, extensions: renderer.extensions ?? [], cellName: cellName(renderer.cell_id) || undefined })),
        handlers: (data.handlers ?? []).filter(handler => !handler.disabled)
            .map(handler => ({ id: handler.id, name: handler.name, cellId: handler.cell_id, folder: handler.folder, cellName: cellName(handler.cell_id) || undefined, types: handler.types })),
    };
}

/** Something the channel already holds that does (part of) the job. A fit
 *  that `ends` finishes the questionnaire through its own action. */
export type Fit =
    | { kind: 'handled'; ext: string; handler: FitHandler; ends: true }
    | { kind: 'shown'; ext: string; renderer: FitRenderer; ends: false }
    | { kind: 'reuse'; ext: string; base: string; renderer: FitRenderer; ends: false }
    | { kind: 'nothing-new'; renderer: FitRenderer; ends: true }
    | { kind: 'change-handler'; handler: FitHandler; ends: true }
    | { kind: 'runner-enough'; generator: FitGenerator; ends: true }
    | { kind: 'runner-of'; generators: FitGenerator[]; ends: false };

export interface RelatedCapability { kind: CapabilityKind; id: string; name: string }

export interface FitAssessment {
    fits: Fit[];
    /** What a work stack should make; empty until the answers say. */
    make: CapabilityKind[];
    related: RelatedCapability[];
}

/** An extension as typed, as the catalogs spell it: lowercase with one
 *  leading dot (`Recipe` → `.recipe`); '' when nothing is typed. */
export function normalizeExt(raw: string | undefined): string {
    const bare = (raw ?? '').trim().toLowerCase().replace(/\s+/g, '').replace(/^\*?\.*/, '');
    return bare ? `.${bare}` : '';
}

/** Every file type the channel knows: renderer extensions and handler types. */
export function knownTypes(catalogs: FitCatalogs): string[] {
    return [...new Set([...catalogs.renderers.flatMap(renderer => renderer.extensions), ...catalogs.handlers.flatMap(handler => handler.types.map(type => type.ext))])].sort();
}

const claimant = (catalogs: FitCatalogs, ext: string) => catalogs.handlers.find(handler => handler.types.some(type => type.ext === ext));
const showing = (catalogs: FitCatalogs, ext: string) => catalogs.renderers.filter(renderer => renderer.extensions.includes(ext));

/** What the answers so far call for. */
export function assessFit(answers: FitAnswers, catalogs: FitCatalogs): FitAssessment {
    const fits: Fit[] = [];
    const related: RelatedCapability[] = [];
    const relate = (kind: CapabilityKind, item: { id: string; name: string }) => {
        if (!related.some(r => r.kind === kind && r.id === item.id)) related.push({ kind, id: item.id, name: item.name });
    };
    let make: CapabilityKind[] = [];
    switch (answers.path) {
        case 'file-type': {
            const ext = normalizeExt(answers.ext);
            if (!ext) break;
            const handler = claimant(catalogs, ext);
            if (handler) {
                // A second handler of the same type would clash with it.
                fits.push({ kind: 'handled', ext, handler, ends: true });
                relate('handler', handler);
                break;
            }
            if (answers.look === 'existing') {
                const as = normalizeExt(answers.lookAs);
                if (!as) break;
                showing(catalogs, as).forEach(renderer => relate('renderer', renderer));
                make = ['handler'];
            } else if (answers.look === 'new-view') make = ['handler', 'renderer'];
            else if (answers.look === 'plain') make = ['handler'];
            break;
        }
        case 'show': {
            const ext = normalizeExt(answers.ext);
            if (!ext) break;
            const renderers = showing(catalogs, ext);
            renderers.forEach(renderer => { fits.push({ kind: 'shown', ext, renderer, ends: false }); relate('renderer', renderer); });
            if (!renderers.length || answers.newView) make = ['renderer'];
            break;
        }
        case 'variant': {
            const base = normalizeExt(answers.base);
            if (!base) break;
            const ownExt = normalizeExt(answers.ownExt);
            const renderer = showing(catalogs, base)[0];
            const handler = claimant(catalogs, base);
            if (renderer) relate('renderer', renderer);
            if (handler) relate('handler', handler);
            if (ownExt) {
                if (renderer) fits.push({ kind: 'reuse', ext: ownExt, base, renderer, ends: false });
                make = ['handler'];
            } else if (answers.checks === false) {
                if (renderer) fits.push({ kind: 'nothing-new', renderer, ends: true });
                else make = ['renderer'];
            } else if (answers.checks === true) {
                if (handler) fits.push({ kind: 'change-handler', handler, ends: true });
                else make = ['handler'];
            }
            break;
        }
        case 'program': {
            if (answers.generator === null) { make = ['generator', 'runner']; break; }
            const generator = catalogs.generators.find(item => item.id === answers.generator);
            if (generator) {
                fits.push({ kind: 'runner-enough', generator, ends: true });
                relate('generator', generator);
            }
            break;
        }
        case 'making': {
            if (answers.how === 'fixed') { make = ['generator', 'runner']; break; }
            if (answers.how !== 'flexible') break;
            const generators = catalogs.generators.filter(generator => catalogs.promptGenerators.includes(generator.id));
            if (generators.length) {
                fits.push({ kind: 'runner-of', generators, ends: false });
                generators.forEach(generator => relate('generator', generator));
            }
            if (!generators.length || answers.noneFit) make = ['generator', 'runner'];
            break;
        }
        case 'unsure':
        case null:
            break;
    }
    return { fits, make, related };
}

/** Whether step 2 is answered far enough to describe what to make: a path
 *  whose answers call for kinds, or 'Not sure' (the work stack decides);
 *  never while a fit ends the questionnaire. */
export function readyToDescribe(answers: FitAnswers, assessment: FitAssessment): boolean {
    if (assessment.fits.some(fit => fit.ends)) return false;
    return answers.path === 'unsure' || assessment.make.length > 0;
}

/** What a fit says. */
export function fitText(fit: Fit): string {
    switch (fit.kind) {
        case 'handled': return `‘${fit.handler.name}’ already handles ${fit.ext} files${fit.handler.cellName ? ` (in ${fit.handler.cellName})` : ''}.`;
        case 'shown': return `‘${fit.renderer.name}’ already shows ${fit.ext} files.`;
        case 'reuse': return `A handler for ${fit.ext} with render_as ${fit.base} reuses ‘${fit.renderer.name}’.`;
        case 'nothing-new': return `Nothing new is needed: ‘${fit.renderer.name}’ already shows it.`;
        case 'change-handler': return `Change ‘${fit.handler.name}’’s guidance and examples.`;
        case 'runner-enough': return `A runner is enough: it saves the program and settings for ‘${fit.generator.name}’.`;
        case 'runner-of': return 'A runner of an existing generator, given written instructions, may be enough:';
    }
}

/** Steps 1 and 2 as asked and answered, in order (the brief's answers). */
export function stepAnswers(answers: FitAnswers, catalogs: FitCatalogs): { question: string; answer: string }[] {
    const lines: { question: string; answer: string }[] = [];
    const path = FIT_PATHS.find(item => item.path === answers.path);
    if (!path) return lines;
    lines.push({ question: FIT_QUESTIONS.path, answer: path.label });
    const push = (question: string, answer: string | undefined) => { if (answer) lines.push({ question, answer }); };
    const generatorName = (id: string) => catalogs.generators.find(generator => generator.id === id)?.name ?? id;
    switch (answers.path) {
        case 'file-type':
            push(FIT_QUESTIONS.ext, normalizeExt(answers.ext));
            push(FIT_QUESTIONS.look, answers.look && LOOK_LABELS[answers.look]);
            if (answers.look === 'existing') push(FIT_QUESTIONS.lookAs, normalizeExt(answers.lookAs));
            break;
        case 'show':
            push(FIT_QUESTIONS.ext, normalizeExt(answers.ext));
            if (answers.newView) push('A view already shows it', 'I still need a new view');
            break;
        case 'variant':
            push(FIT_QUESTIONS.base, normalizeExt(answers.base));
            push(FIT_QUESTIONS.ownExt, normalizeExt(answers.ownExt));
            push(FIT_QUESTIONS.checks, answers.checks === undefined ? undefined : answers.checks ? 'Yes' : 'No');
            break;
        case 'program':
            push(FIT_QUESTIONS.generator, answers.generator === null ? 'None of these' : answers.generator ? generatorName(answers.generator) : undefined);
            break;
        case 'making':
            push(FIT_QUESTIONS.how, answers.how && HOW_LABELS[answers.how]);
            if (answers.noneFit) push('An existing generator with instructions', 'None fit');
            break;
        case 'unsure':
        case null:
            break;
    }
    return lines;
}
