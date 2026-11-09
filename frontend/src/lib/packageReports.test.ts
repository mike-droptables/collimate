import { describe, expect, it, vi } from 'vitest';

const openAlert = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock('@/stores/useCollimateStore', () => ({ useCollimateStore: { getState: () => ({ openAlert }) } }));

import { reportRepositorySkips } from './packageReports';

describe('reporting a new channel\'s repositories left behind', () => {
    it('names each repository and why, and says nothing when none was left behind', async () => {
        await reportRepositorySkips([]);
        await reportRepositorySkips(undefined);
        expect(openAlert).not.toHaveBeenCalled();
        await reportRepositorySkips([{ path: 'Repo/proj/.git', reason: 'repository history not copied: git is not available on this host' }]);
        expect(openAlert).toHaveBeenCalledWith({
            title: 'Repository history not kept',
            body: 'Repo/proj/.git: repository history not copied: git is not available on this host',
        });
    });
});
