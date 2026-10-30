import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import vectors from '../../../api/tests/fixtures/run-log-vectors.json';
import { formatLogLine } from './runLogFormat';

// The run-log contract against the shared golden vectors: this formatter and
// api/process_manager/log_format.py stay two implementations, and both
// suites read api/tests/fixtures/run-log-vectors.json (the other is
// api/tests/test_pytest_log_format.py), so the live drawer and the persisted
// logs.txt produce identical lines.
//
// Timestamps render in LOCAL time, so the vectors fix the zone: this file
// sets TZ to the fixture's `timezone` for its tests and restores it.
const zone = process.env.TZ;
beforeAll(() => { process.env.TZ = vectors.timezone; });
afterAll(() => {
    if (zone === undefined) delete process.env.TZ;
    else process.env.TZ = zone;
});

describe('formatLogLine: the shared run-log golden vectors', () => {
    it.each(vectors.cases)('$name', row => {
        expect(formatLogLine(row.event_type, row.payload, row.created_at)).toBe(row.line);
    });
});
