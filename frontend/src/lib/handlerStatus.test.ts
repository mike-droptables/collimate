import { describe, expect, it } from 'vitest';
import type { HandlerInfo } from './definitionApi';
import { ACTION_LABELS, clashLine, extPhrase, HANDLER_MODEL, handlerState, handlerStatus, hint, nextLabel, short, type HandlerState } from './handlerStatus';

const record = (over: Partial<HandlerInfo> = {}): HandlerInfo => ({
    id: 'memo', name: 'Memo', description: '', types: [{ ext: '.memo' }, { ext: '.mm' }], examples: [], cell_id: 'cell',
    folder: 'memo', definition_path: 'memo/memo.handler', has_derived: false, built_hash: null, accepted: false,
    accepted_derived: null, disabled: false, clashes: [], accepted_skills: [], added_skills: [], builtin: false,
    uses_app_skills: false, ...over,
});
const BUILT = 'b1b2b3b4b5b6b7b8';
const USED = 'a1a2a3a4a5a6a7a8';

describe('handler status words', () => {
    const cases: (HandlerState & { over: Partial<HandlerInfo> })[] = [
        {
            key: 'off', over: { disabled: true }, label: 'Off', tone: 'red', next: 'edit',
            detail: 'Not used: another handler in this channel has the id ‘memo’. Its files are handled as if it did not exist.',
        },
        {
            key: 'none', over: {}, label: 'No checks yet', tone: 'muted', next: 'build',
            detail: 'Files ending .memo or .mm are saved unchecked, and agents get no skills for them. Build opens a work stack that writes its checks and skills.',
        },
        {
            key: 'review-first', over: { has_derived: true, built_hash: BUILT }, label: 'Build waiting for review', tone: 'brass', next: 'review',
            detail: 'Build b1b2b3b4 is ready. Until you accept it, .memo or .mm files stay unchecked and agents get no skills.',
        },
        {
            key: 'in-use', over: { has_derived: true, built_hash: USED, accepted: true, accepted_derived: USED, accepted_skills: ['memo-writing'], added_skills: ['house-style'] },
            label: 'In use', tone: 'green', next: null,
            detail: 'Build a1a2a3a4: its checks run on every .memo or .mm file and agents get its skills (memo-writing + added house-style).',
        },
        {
            key: 'review-next', over: { has_derived: true, built_hash: BUILT, accepted_derived: USED }, label: 'In use · new build waiting', tone: 'green-waiting', next: 'review',
            detail: 'Build a1a2a3a4 stays in use until you accept build b1b2b3b4.',
        },
        {
            key: 'orphaned', over: { accepted_derived: USED }, label: 'In use · build removed from its folder', tone: 'green', next: 'build',
            detail: 'Build a1a2a3a4 is still in use, but its files are gone from the handler’s folder. Build again to change it.',
        },
        {
            key: 'app', over: { builtin: true, accepted: true, uses_app_skills: true, accepted_skills: ['memo-writing'] }, label: 'App’s checks and skills', tone: 'green', next: null,
            detail: 'The app checks every .memo or .mm file and agents get the app’s skills (memo-writing).',
        },
        {
            key: 'channel', over: { builtin: true, accepted: true, accepted_skills_hash: USED }, label: 'App’s checks · this channel’s skills', tone: 'green', next: null,
            detail: 'The app checks .memo or .mm files. Agents get the skills this channel regenerated (skills a1a2a3a4).',
        },
        {
            key: 'regen-waiting', over: { builtin: true, has_derived: true, built_hash: BUILT, uses_app_skills: true }, label: 'Regenerated skills waiting for review', tone: 'green-waiting', next: 'review',
            detail: 'Agents still get the app’s skills until you accept skills b1b2b3b4. The checks never change.',
        },
    ];
    it.each(cases)('$key: label, detail, next action and tone', ({ key, over, label, detail, next, tone }) => {
        expect(handlerState(record(over))).toEqual({ key, label, detail, next, tone });
    });

    it('a regeneration waiting over an earlier accepted one says those skills stay in use', () => {
        expect(handlerState(record({ builtin: true, has_derived: true, built_hash: BUILT, accepted_skills_hash: USED })).detail)
            .toBe('Agents still get this channel’s earlier skills until you accept skills b1b2b3b4. The checks never change.');
    });

    it('names the next action as the card’s Next line does; off asks for its id', () => {
        expect(nextLabel(record())).toBe('Build');
        expect(nextLabel(record({ has_derived: true, built_hash: BUILT }))).toBe('Review and accept');
        expect(nextLabel(record({ disabled: true }))).toBe('Edit its id');
        expect(nextLabel(record({ accepted: true, accepted_derived: USED, has_derived: true, built_hash: USED }))).toBeNull();
    });

    it('a clash adds a red line and the dot title carries it', () => {
        const clashing = record({ clashes: ['.mm'] });
        expect(clashLine(clashing)).toBe('Shares .mm with another handler, so neither handles .mm files.');
        expect(clashLine(record())).toBeNull();
        expect(handlerStatus(clashing)).toBe('No checks yet. Shares .mm with another handler, so neither handles .mm files.');
        expect(handlerStatus(record())).toBe('No checks yet');
    });

    it('names builds by 8 characters and types as a phrase', () => {
        expect(short(BUILT)).toBe('b1b2b3b4');
        expect(short(null)).toBe('');
        expect(extPhrase(record({ types: [{ ext: '.a' }] }))).toBe('.a');
        expect(extPhrase(record({ types: [{ ext: '.a' }, { ext: '.b' }, { ext: '.c' }] }))).toBe('.a, .b or .c');
    });

    it('fills the types into the hints that name them, and words Validate for whose checks run', () => {
        expect(hint('review', record())).toBe('Shows the waiting build’s checks and example results, then puts it in use for every .memo or .mm file and agent.');
        expect(hint('validate', record())).toContain('checks of the latest build');
        expect(hint('validate', record({ builtin: true }))).toContain('the app’s checks');
        expect(hint('delete', record())).toBe('Removes this handler’s folder (definition, examples and builds) from its cell. Its files are no longer checked.');
    });

    it('Build and Regenerate open a work stack, and the Next line drops the button’s ellipsis', () => {
        expect(ACTION_LABELS.build).toBe('Build…');
        expect(ACTION_LABELS.regenerate).toBe('Regenerate skills…');
        expect(hint('build', record())).toMatch(/^Opens a work stack with a copy of this handler/);
        expect(hint('regenerate', record({ builtin: true }))).toMatch(/^Opens a work stack with this handler, this channel’s examples/);
        expect(nextLabel(record({ accepted_derived: USED }))).toBe('Build');
    });

    it('says where a handler lives: Open cell for a user handler, Open card for a built-in', () => {
        expect(ACTION_LABELS.openCell).toBe('Open cell');
        expect(ACTION_LABELS.openCard).toBe('Open card');
        expect(hint('openCell', record())).toBe('Brings the cell that holds this handler to the front of Capabilities.');
        expect(hint('openCard', record({ builtin: true }))).toBe('Brings the built-in card that holds this handler to the front of Capabilities.');
    });

    it('the model line says builds come from a work stack and wait for review', () => {
        expect(HANDLER_MODEL).toContain('Build opens a work stack');
        expect(HANDLER_MODEL).toContain('review and accept it here');
    });
});
