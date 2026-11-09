// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';

const store = vi.hoisted(() => ({
    openChoice: vi.fn(async () => 'duplicate' as string | null),
    openAlert: vi.fn(async () => true),
    applyOptimisticChannel: vi.fn(),
    requestSelectCell: vi.fn(),
    activeChannel: { id: 'ch' },
    runs: {} as Record<string, { runId: string; cellId: string }>,
}));
vi.mock('@/stores/useCollimateStore', () => ({ useCollimateStore: { getState: () => store } }));
vi.mock('@/lib/api/cells', () => ({ duplicateCell: vi.fn(async () => ({ ok: true, new_stack_id: 's2', new_cell_id: 'copy', channel: { id: 'ch' } })) }));

import { duplicateCell } from '@/lib/api/cells';
import { offerDuplicate, refusalText, repositoryBusyMessage, reportFileError } from './repositoryBusy';

const MESSAGE = 'git is in use by run abcd1234 on this cell; Duplicate the cell for a second working copy';
const refused = (status: number, data: unknown) => Object.assign(new Error('Request failed'), { response: { status, data } });

beforeEach(() => { vi.clearAllMocks(); store.runs = {}; store.openChoice.mockResolvedValue('duplicate'); });

// One run writes a cell's git at a time; every refusal of the lease offers
// a second working copy (§4.1, R3).
describe('a repository-lease refusal', () => {
    it('is read from a repository_busy body, a folder operation’s detail, or the error it caused', () => {
        expect(repositoryBusyMessage(refused(409, { error: 'repository_busy', owner: 'git-abcd1234', run_id: 'abcd1234', message: MESSAGE }))).toBe(MESSAGE);
        expect(repositoryBusyMessage(refused(409, { detail: MESSAGE }))).toBe(MESSAGE);
        expect(repositoryBusyMessage(new Error(MESSAGE, { cause: refused(409, { detail: MESSAGE }) }))).toBe(MESSAGE);
        expect(repositoryBusyMessage(refused(409, { detail: "The folder's files changed ('a/x' is not part of the operation); reload and retry" }))).toBeNull();
        expect(repositoryBusyMessage(refused(503, { error: 'repository_busy', message: MESSAGE }))).toBeNull();
        expect(repositoryBusyMessage(new Error(MESSAGE))).toBeNull();
    });

    it('the git routes’ own message wins over the status line', () => {
        expect(refusalText(refused(503, { error: 'git_unavailable', message: 'git on this machine is missing' }))).toBe('git on this machine is missing');
        expect(refusalText(refused(404, { detail: 'Not here' }))).toBe('Not here');
    });

    it('Duplicate copies the cell and selects the copy; closing the offer does nothing', async () => {
        await offerDuplicate('c1', MESSAGE);
        expect(store.openChoice).toHaveBeenCalledWith({ title: 'Git is in use', body: MESSAGE, options: [{ id: 'duplicate', label: 'Duplicate cell' }] });
        expect(duplicateCell).toHaveBeenCalledWith('ch', 'c1');
        expect(store.applyOptimisticChannel).toHaveBeenCalledWith({ id: 'ch' });
        expect(store.requestSelectCell).toHaveBeenCalledWith('copy');
        store.openChoice.mockResolvedValueOnce(null);
        await offerDuplicate('c1', MESSAGE);
        expect(duplicateCell).toHaveBeenCalledTimes(1);
    });

    it('a refused file operation offers to duplicate the cell whose run holds the git; others are alerts', async () => {
        store.runs = { 'abcd1234-ffff': { runId: 'abcd1234-ffff', cellId: 'target' } };
        await reportFileError('source', 'Move failed', refused(409, { detail: MESSAGE }));
        expect(duplicateCell).toHaveBeenCalledWith('ch', 'target');
        await reportFileError('source', 'Delete failed', Object.assign(new Error("A repository cannot live at 'x/_runs'"), { response: { status: 409, data: { detail: "A repository cannot live at 'x/_runs'" } } }));
        expect(store.openAlert).toHaveBeenCalledWith({ cellId: 'source', title: 'Delete failed', body: "A repository cannot live at 'x/_runs'" });
    });
});
