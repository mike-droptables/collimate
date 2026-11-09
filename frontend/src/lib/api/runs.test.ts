import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { stopChannelRuns, stopRun } from './runs';

describe('the run stop routes', () => {
    afterEach(() => vi.restoreAllMocks());

    it('stop one run, or every live run of one channel, by its method and path', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { requested: 2 } });
        await stopRun('c 1', 'r1');
        expect(await stopChannelRuns('c 1')).toEqual({ requested: 2 });
        expect(request.mock.calls.map(([config]) => [config.method, config.url])).toEqual([
            ['DELETE', '/channels/c%201/runs/r1'],
            ['POST', '/channels/c%201/runs/stop_all'],
        ]);
    });
});
