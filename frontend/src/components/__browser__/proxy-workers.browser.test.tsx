import {expect, it} from 'vitest';
import {removeRunProxyWorkers} from '@/lib/proxyWorkers';

it('Chromium permits a run scope, refuses app scope, and removes only the ended run registrations P4F06_F1935', async () => {
  const registrations: ServiceWorkerRegistration[] = [];
  const base = '/proxy/dddddddd-dddd-4ddd-8ddd-dddddddddddd/';
  try {
    for (const path of ['ended/5100/', 'ended/5101/', 'live/5100/']) {
      registrations.push(await navigator.serviceWorker.register(`${base}${path}nested/sw.js`, {scope: `${base}${path}`}));
    }
    await expect(navigator.serviceWorker.register(`${base}ended/5100/nested/sw.js`, {scope: '/'})).rejects.toThrow();
    await removeRunProxyWorkers('dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'ended');
    const scopes = (await navigator.serviceWorker.getRegistrations()).map(registration => new URL(registration.scope).pathname);
    expect(scopes).not.toContain(`${base}ended/5100/`);
    expect(scopes).not.toContain(`${base}ended/5101/`);
    expect(scopes).toContain(`${base}live/5100/`);
  } finally {
    await Promise.all(registrations.map(registration => registration.unregister()));
  }
});
