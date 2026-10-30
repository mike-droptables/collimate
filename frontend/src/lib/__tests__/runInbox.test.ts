// The approval-card half of a run's inbox: card normalisation, the frames
// that change the cards (and the ones that never do), the answer frames,
// the socket URL and the redial wait.

import { describe, expect, it } from 'vitest';
import { answerFrame, applyInboxFrame, inboxRedialMs, inboxSocketUrl, normalizePermission, type PendingPermission } from '../runInbox';

const card = (id: string, title = 'Run a command'): PendingPermission => ({
    id, title, kind: 'execute', options: [{ option_id: 'allow', name: 'Allow', kind: 'allow_once' }],
});

describe('normalizePermission', () => {
    it('needs an id, fills the defaults and keeps only options with an id', () => {
        expect(normalizePermission({ title: 'x' })).toBeNull();
        expect(normalizePermission(null)).toBeNull();
        expect(normalizePermission('x')).toBeNull();
        expect(normalizePermission({ id: 7, options: [{ option_id: 'a' }, { name: 'no id' }, null] })).toEqual({
            id: '7', title: 'Permission', kind: '', options: [{ option_id: 'a', name: '', kind: '' }],
        });
        expect(normalizePermission({ id: 'p', title: 'T', kind: 'edit', detail: 'rm -rf', options: 'x' }))
            .toEqual({ id: 'p', title: 'T', kind: 'edit', detail: 'rm -rf', options: [] });
    });
});

describe('applyInboxFrame', () => {
    const cards = [card('a'), card('b')];

    it('a history frame replaces the cards with its pending requests', () => {
        expect(applyInboxFrame(cards, { t: 'history', pending_requests: [{ id: 'c', title: 'C' }, { title: 'no id' }] }).map(c => c.id)).toEqual(['c']);
        expect(applyInboxFrame(cards, { t: 'history', text: 'replayed text' })).toBe(cards);
    });

    it('a request appends or replaces by id; a resolution removes', () => {
        expect(applyInboxFrame(cards, { t: 'permission_request', turn_id: 't', id: 'c', title: 'C' }).map(c => c.id)).toEqual(['a', 'b', 'c']);
        const replaced = applyInboxFrame(cards, { t: 'permission_request', id: 'a', title: 'Again' });
        expect(replaced.map(c => [c.id, c.title])).toEqual([['a', 'Again'], ['b', 'Run a command']]);
        expect(applyInboxFrame(cards, { t: 'permission_request', title: 'no id' })).toBe(cards);
        expect(applyInboxFrame(cards, { t: 'permission_resolved', id: 'a' }).map(c => c.id)).toEqual(['b']);
        expect(applyInboxFrame(cards, { t: 'permission_resolved', id: 'zz' })).toBe(cards);
    });

    it('any other frame (a replayed text delta, a turn bracket, garbage) leaves the cards untouched', () => {
        for (const frame of [{ t: 'delta', turn_id: 't', text: 'hello' }, { t: 'turn_start' }, { t: 'tool', id: 'a' }, null, 'x', 3]) {
            expect(applyInboxFrame(cards, frame)).toBe(cards);
        }
    });
});

describe('the wire out', () => {
    it('answers with an option or cancels', () => {
        expect(answerFrame('p', 'allow')).toEqual({ t: 'permission_answer', id: 'p', option_id: 'allow' });
        expect(answerFrame('p', null)).toEqual({ t: 'permission_answer', id: 'p', cancelled: true });
    });

    it('dials the API origin in ws or wss with the token', () => {
        expect(inboxSocketUrl('/api/chats/c1', 'a b', '../api', 'http://host:8000/app/')).toBe('ws://host:8000/api/chats/c1?token=a%20b');
        expect(inboxSocketUrl('/api/chats/c1', 't', 'https://api.example.test/api', 'http://page.test/')).toBe('wss://api.example.test/api/chats/c1?token=t');
    });

    it('waits longer after each failed dial, up to 5 s', () => {
        expect([1, 2, 9, 10, 40].map(inboxRedialMs)).toEqual([500, 1000, 4500, 5000, 5000]);
    });
});
