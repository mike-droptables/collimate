// @vitest-environment jsdom
import {afterEach, expect, it, vi} from 'vitest';
import {proxyWorkers, removeRunProxyWorkers} from './proxyWorkers';

afterEach(() => vi.unstubAllGlobals());

it('only removes the exact channel UUID and run scope P4F06', async () => {
  const paths = ['/proxy/Shared%20label/ended/5100/', '/proxy/11111111-1111-4111-8111-111111111111/ended/5101/nested/',
    '/proxy/11111111-1111-4111-8111-111111111111/ended-more/5100/', '/proxy/other/ended/5100/',
    '/proxy/11111111-1111-4111-8111-111111111111/ended/no-port/', '/proxy/11111111-1111-4111-8111-111111111111/ended/5100', '/'];
  const registrations = paths.map(path => ({scope: new URL(path, location.origin).href, unregister: vi.fn(async () => true)}));
  registrations.push({scope: 'https://foreign.test/proxy/11111111-1111-4111-8111-111111111111/ended/5100/', unregister: vi.fn(async () => true)});
  registrations[1].unregister.mockRejectedValue(new Error('synthetic refusal'));
  vi.stubGlobal('navigator', {serviceWorker: {getRegistrations: vi.fn(async () => registrations)}});
  await removeRunProxyWorkers('11111111-1111-4111-8111-111111111111', 'ended');
  expect(registrations.map(registration => registration.unregister.mock.calls.length)).toEqual([0, 1, 0, 0, 0, 0, 0, 0]);
});

it('unsupported and refused worker enumeration leaves run recovery available P4F06', async () => {
  vi.stubGlobal('navigator', {});
  expect(await proxyWorkers('ch')).toEqual([]);
  vi.stubGlobal('navigator', {serviceWorker: {getRegistrations: vi.fn(async () => {throw new Error('disabled');})}});
  expect(await proxyWorkers('ch')).toEqual([]);
});
